/*
	Kage game server.
	Copyright 2019 Shuouma <dreamcast-talk.com>
    Copyright 2025 Flyinghead <flyinghead.github@gmail.com>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
*/
#include "bomberman.h"
#include "log.h"
#include <numeric>

using namespace std::chrono_literals;

BMRoom::BMRoom(Lobby& lobby, uint32_t id, const std::string& name, uint32_t attributes, Player *owner, asio::io_context& io_context)
	: Room(lobby, id, name, attributes, owner, io_context), timer(io_context)
{
	// needed after the owner is added in the parent constructor
	updateSlots();
}

void BMRoom::addPlayer(Player *player) {
	Room::addPlayer(player);
	updateSlots();
}
bool BMRoom::removePlayer(Player *player)
{
	bool b = Room::removePlayer(player);
	updateSlots();
	return b;
}
uint32_t BMRoom::getPlayerCount() const {
	return std::accumulate(slots.begin(), slots.end(), 0);
}

void BMRoom::sendUdpPacketA(Packet& packet, Player *player)
{
	UdpCommand cmd {};
	cmd.command = 0xA;		// player joined?
	packet.init(Packet::REQ_CHAT);
	packet.flags |= Packet::FLAG_RUDP;
	packet.writeData(cmd.full);
	packet.writeData((uint16_t)0);			// flag?

	packet.writeData(getHostCount());
	for (const Player *pl : players)
	{
		packet.writeData(pl->getId());		// player kage id
		uint32_t slots = (uint32_t)getSlotCount(pl);
		packet.writeData(slots);				// guest+1 count
		uint32_t pos = (uint32_t)getPlayerPosition(pl);
		for (unsigned i = 0; i < slots; i++)
			// playerId [0-7]
			packet.writeData(pos + i);		// FIXME different from udp_8 but looks better.
	}
}

// owner: needs pkt8 only at creation
//        pktA when player joins seems to work fine
// joiner: pkt8 fills joining player slots
//         but pktA doesn't work -> player occupies all slots
void BMRoom::createJoinRoomReply(Packet& reply, Packet& relay, Player *player)
{
	UdpCommand cmd{};
	cmd.command = 8;	// player list?
	reply.init(Packet::REQ_CHAT);
	reply.flags |= Packet::FLAG_RUDP | Packet::FLAG_CONTINUE;
	reply.writeData(cmd.full);
	reply.writeData((uint16_t)0);			// flag?
	reply.writeData(player->getId());		// player kage id
	reply.writeData((uint32_t)getPlayerIndex(player));		// FIXME p[915] [0-F]? client id?
	uint32_t pos = (uint32_t)getPlayerPosition(player);
	reply.writeData(pos); 					// player pos
	uint32_t slots = getSlotCount(player);
	reply.writeData(slots - 1);				// [911] guest count
	reply.writeData(owner->getId());		// room owner kage id
	reply.writeData((uint32_t)getPlayerPosition(owner)); // room owner player pos
	// for each player: -1 or player pos (1 - 8)
	reply.writeData(++pos);
	for (unsigned i = 1; i < slots; i++)
		reply.writeData(++pos);

	// FIXME trying to send A after that => current player now occupies 2 slot groups...
	// not needed for owner at creation
	if (player != owner)
		sendUdpPacketA(reply, player);

	// FIXME works for the room members, but also needed for the joining player?
	sendUdpPacketA(relay, player);
	// tried C,D no effect?
	// F -> game responds with accept new rules
	// 10 no effect
	// 12 -> a game could not be started
	// E -> new room master (doesn't seem required)
/*
	if (player == owner)
	{
		cmd.command = 0xE;	// new room master
		reply.init(Packet::REQ_CHAT);
		reply.flags |= Packet::FLAG_RUDP;
		reply.writeData(cmd.full);
		reply.writeData((uint16_t)0);
		reply.writeData(owner->getId());		// room owner kage id
		reply.writeData((uint32_t)getPlayerPosition(owner)); // room owner player pos
	}
*/
}

int BMRoom::getSlotCount(const Player *player) const
{
	int idx = getPlayerIndex(player);
	if (idx < 0)
		return 0;
	return slots[idx];
}

int BMRoom::getPlayerPosition(const Player *player) const
{
	int idx = getPlayerIndex(player);
	if (idx < 0)
		return idx;
	return std::accumulate(slots.begin(), slots.begin() + idx, 0);
}

void BMRoom::updateSlots()
{
	slots.clear();
	for (const Player *player : players)
	{
		const auto& extra = player->getExtraData();
		int slotCount = read32(extra.data(), 0) + 1;
		slots.push_back(slotCount);
	}
}


BombermanServer::BombermanServer(uint16_t port, asio::io_context& io_context)
	: LobbyServer(Game::Bomberman, port, io_context)
{
}

bool BombermanServer::handlePacket(Player *player, const uint8_t *data, size_t len)
{
	// TODO Should go to lobby server?
	uint16_t flags = read16(data, 0);
	if (flags & Packet::FLAG_ACK)
		player->ackRUdp(read32(data, 0xc));

	BMRoom *room = (BMRoom *)player->getRoom();
	if (room != nullptr)
	{
		if (joinRoomTime == time_point{}) {
			joinRoomTime = Clock::now();
		}
		else if (Clock::now() >= joinRoomTime + 6s)
		{
			// seems to help display the user list but name is "$"...
			// message 1400:user list?
			// 14: player count
			// (for each)
			// 18: kage id
			// 1c: pos? (> 0) player count for this slot? (1 + guests?)
			// (for each count)
			// 20: index? [0-7]
			/*
			replyPacket.init(Packet::REQ_CHAT);
			replyPacket.flags |= Packet::FLAG_RUDP;
			replyPacket.writeData((uint16_t)0x1400);	// user list?
			replyPacket.writeData((uint16_t)0);			// flag?
			replyPacket.writeData(1u);					// player count
			replyPacket.writeData(player->getId());		// P1 id
			replyPacket.writeData(1u);					// P1 1 + guests
			replyPacket.writeData(0u);					// P1 pos
			*/
			// unlocks the room UI after creation.
			// doesn't know its own user-id? now says self=ff000000
			// pos: now says self=0
/*
			replyPacket.init(Packet::REQ_CHAT);
			replyPacket.flags |= Packet::FLAG_RUDP;
			replyPacket.writeData((uint16_t)0x1c00);	// new key holder
			replyPacket.writeData((uint16_t)0);			// flag?
			replyPacket.writeData(player->getId());
			replyPacket.writeData(0u);					// pos. in room
*/
			joinRoomTime = time_point{};

		}
		else if (Clock::now() >= joinRoomTime + 3s)
		{
			// work: calling kickoutFromRoom_maybe()
			/*
			printf("Kicking player out\n");
			replyPacket.init(Packet::REQ_CHAT);
			replyPacket.flags |= Packet::FLAG_RUDP;
			replyPacket.writeData((uint16_t)0x0E00);	// kick player
			replyPacket.writeData((uint16_t)0x0080);	// flag
			uint32_t id = player->getRoom()->getId();
			replyPacket.writeData((uint8_t *)&id, 4);	// LE or BE??
			*/
			// unlocks the room UI after creation.
			// doesn't know its own user-id? (says self=0)
			// still waiting for own pos. in room? (says self=-1)
			/*
			replyPacket.init(Packet::REQ_CHAT);
			replyPacket.flags |= Packet::FLAG_RUDP;
			replyPacket.writeData((uint16_t)0x1c00);	// new key holder
			replyPacket.writeData((uint16_t)0);			// flag?
			replyPacket.writeData(player->getId());
			replyPacket.writeData(0u);					// pos. in room
			*/

			// message: returned to game room due to lack of players
			// still doesn't show player names
			// crashes if player/guest count==0 and no extra data at 2c
/* FIXME wrong
			Room *room = player->getRoom();
			replyPacket.init(Packet::REQ_CHAT);
			replyPacket.flags |= Packet::FLAG_RUDP;
			replyPacket.writeData((uint16_t)0x1000);	// player info?
			replyPacket.writeData((uint16_t)0);			// flag?
			replyPacket.writeData(player->getId());		// player kage id
			replyPacket.writeData(0u);					// p[915] ? client id?
			replyPacket.writeData(0u);					// player pos
			replyPacket.writeData(0u); 					// [911] guest count
			replyPacket.writeData(player->getId());		// room owner kage id
			replyPacket.writeData(0u);					// room owner player pos
			// for each player: -1 or player pos(?)
			replyPacket.writeData(0u);
//			for (unsigned i = 1; i < room->getMaxPlayers(); i++)
//				replyPacket.writeData(0xffffffffu);
 */
		}
	}

	uint16_t subtype = read16(data, 0x10);
	UdpCommand cmd;
	cmd.full = subtype;
	if (data[3] == Packet::REQ_GAME_DATA)
	{
		switch (cmd.command)
		{
		case 7:		// Set game rules
			DEBUG_LOG(Game::Bomberman, "%s: set game rules", player->getName().c_str());
			replyPacket.init(Packet::REQ_NOP);
			replyPacket.ack(read32(data, 8));

			if (room != nullptr)
			{
				room->setRules(&data[0x14]);
/*
				relayPacket.init(Packet::REQ_CHAT);
				relayPacket.flags |= Packet::FLAG_RUDP;
				relayPacket.writeData(cmd.full);
				relayPacket.writeData(read16(data, 0x12));
				relayPacket.writeData(room->getRules().data(), 9);
*/
			}
			break;

		//case 9:	// ?
		// 2 ints per slot: be room pos [0-7], be p[8bf + pos * 5]	-> set by msg C
		// only sent by owner?
		// NOT USED...

		case 0xa:	// Start battle
			DEBUG_LOG(Game::Bomberman, "%s: START BATTLE!", player->getName().c_str());
			replyPacket.respOK(Packet::REQ_CHAT);
			replyPacket.ack(read32(data, 8));

			relayPacket.init(Packet::REQ_CHAT);
			relayPacket.flags |= Packet::FLAG_RUDP;
			// TODO udp command?
			break;

		case 0xb:	// Agree new rules
			DEBUG_LOG(Game::Bomberman, "%s: agree new rules", player->getName().c_str());
			if (room != nullptr)
			{
				replyPacket.init(Packet::REQ_NOP);
				if (replyPacket.size == 0x10)
					// FIXME how to figure out if we need to ack it or not?
					replyPacket.ack(read32(data, 8));
/*
				replyPacket.init(Packet::REQ_CHAT);
				replyPacket.flags |= Packet::FLAG_RUDP;
				if (replyPacket.size == 0x10)
					// FIXME how to figure out if we need to ack it or not?
					replyPacket.ack(read32(data, 8));
				replyPacket.writeData(cmd.full);
				replyPacket.writeData(read16(data, 0x12));
				replyPacket.writeData(room->getRules().data(), 9);
*/
				if (room->getOwner() == player)
				{
					// TODO do this when owner sets new rules
					relayPacket.init(Packet::REQ_CHAT);
					relayPacket.flags |= Packet::FLAG_RUDP;
					relayPacket.writeData(cmd.full);
					relayPacket.writeData(read16(data, 0x12));
					relayPacket.writeData(room->getRules().data(), 9);
				}
				else
				{
					// TODO notify owner that rules have been accepted
					Packet pkt;
					pkt.init(Packet::REQ_CHAT);
					pkt.flags |= Packet::FLAG_RUDP;
					cmd.command = 0xC;	// F, 10 not working, 17 responds with udp11 subF (no payload)
					cmd.size = 0;	// TODO?
					pkt.writeData(cmd.full);
					pkt.writeData((uint16_t)0);
					const auto& players = room->getPlayers();
					pkt.writeData((uint32_t)players.size());
					for (Player *pl : players)
					{
						pkt.writeData(pl->getId());	// FIXME or room pos?
						uint32_t slots = room->getSlotCount(pl);
						uint32_t pos = room->getPlayerPosition(pl);
						pkt.writeData(slots);
						for (uint32_t i = 0; i < slots; i++) {
							pkt.writeData(pos + i);
							pkt.writeData((uint32_t)0xff);
						}
					}
					room->getOwner()->send(pkt);
				}
			}
			break;

		case 0xc:	// Received new rules?
			{
				DEBUG_LOG(Game::Bomberman, "%s: received new rules", player->getName().c_str());
				replyPacket.init(Packet::REQ_NOP);
				replyPacket.ack(read32(data, 8));
/*
				// doesn't look right: message "the room master has set new rules"
				Packet pkt;
				pkt.init(Packet::REQ_CHAT);
				pkt.flags |= Packet::FLAG_RUDP;
				pkt.writeData(cmd.full);
				pkt.writeData(read16(data, 0x12));
				room->getOwner()->send(pkt);
*/
				break;
			}

		case 0xf:	// ??? response to udpF 17
			replyPacket.init(Packet::REQ_NOP);
			replyPacket.ack(read32(data, 8));

			relayPacket.init(Packet::REQ_CHAT);
			relayPacket.flags |= Packet::FLAG_RUDP;
			relayPacket.writeData(cmd.full);
			relayPacket.writeData(read16(data, 0x12));
			break;

		// 1A, 1B: SendGameMapBlock: Send MapInfo

		default:
			ERROR_LOG(game, "Unhandled udp 11 command: %x (%04x)", cmd.command, cmd.full);
			dumpData(data, len);
			return false;
		}
		return true;
	}
	if (data[3] != Packet::REQ_CHAT || (flags & Packet::FLAG_RELAY))
		return false;

	switch (cmd.command)
	{
	//case 4:		// Start_SyncTimer

	case 7:		// kick player
		replyPacket.init(Packet::REQ_NOP);
		replyPacket.ack(read32(data, 8));
		if (room != nullptr)
		{
			uint32_t playerPos = data[0x14];
			for (Player *player : room->getPlayers())
			{
				if (room->getPlayerPosition(player) == (int)playerPos)
				{
					Packet pkt;
					pkt.init(Packet::REQ_CHAT);
					pkt.flags |= Packet::FLAG_RUDP;
					pkt.writeData(&data[0x10], 4);
					pkt.writeData((const uint8_t *)&playerPos, sizeof(playerPos));
					player->send(pkt);
					break;
				}
			}
		}
		break;

	case 0x1C: // PING
		DEBUG_LOG(Game::Bomberman, "%s: ping", player->getName().c_str());
		// this is correctly received as a ping
		replyPacket.init(Packet::REQ_CHAT);
		replyPacket.writeData(cmd.full);
		replyPacket.writeData((uint16_t)0);
		replyPacket.writeData(0x10000000u);	// LE int. ping value? only lsbyte used. 1,4,10,80,c8 is red
		replyPacket.writeData((uint8_t)data[0x18]); // bitfield (8 flags) one per player sharing the same connection
		break;

	default:
		ERROR_LOG(game, "Unhandled udp F command: %x (%04x)", cmd.command, cmd.full);
		dumpData(data, len);
		return false;
	}

	return true;
}

Room *BombermanServer::addRoom(const std::string& name, uint32_t attributes, Player *owner)
{
	uint32_t id = nextRoomId++;
	BMRoom *room = new BMRoom(*owner->getLobby(), id, name, attributes, owner, io_context);
	owner->getLobby()->addRoom(room);

	return room;
}
