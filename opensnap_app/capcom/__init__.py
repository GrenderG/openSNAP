"""Capcom APP: the TCP service Capcom's SN@P titles use beside SN@P.

It is Capcom's own protocol, not part of SN@P: its own host
(`app01.reo.capcom.sf.yav4.com:10127`), framing and field codec, and client
code outside the kkClient SDK. One process serves every Capcom title:
`protocol`, `connection` and `server` are shared, and each title has its own
package (`monsterhunter`, `outbreak`).
"""
