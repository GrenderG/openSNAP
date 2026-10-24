# Monster Hunter client patches

This folder holds the code patches the Capcom APP service can offer to Monster
Hunter clients (6001/6002). `patches.json` lists each blob with its 6001 name
and version, the client versions it is offered to, whether it is `enabled`,
and its `changelog`. The blobs are written by
`tools/monsterhunter/build_eu_patch.py` and `build_na_patch.py`; the format
and install flow are documented in
`opensnap_app/capcom/monsterhunter/patch.py`.

`eu_official_1001.bin` is Capcom's own EU patch 1001 (name `0509261742`),
decrypted from a retail game save: viciousShadow's original save data, thanks
to whom it could be extracted. It is kept for reference and as the source
of its two fixes, which `build_eu_patch.py` copies into `eu_custom_1002.bin`. EU 1002
is also offered to clients that report `0509261742`, the clients with Capcom's
patch installed.

## The current blobs do not work

`eu_custom_1002.bin` and `na_custom_1002.bin` are raw `M-HUNTER` patches with no DNAS
install header, so retail EU (`SLES_527.07`) and NA (`SLUS_208.96`) cannot
install them. Their entries are therefore `"enabled": false` and the server
does not offer them. They are kept because the patch content itself is correct
and ready for the day a signed header is available.

## Why a raw blob is rejected

Before saving a downloaded patch, retail clients pass it through the DNAS2
install library (`dnas_ins` overlay, `InetDNAS2PersonalEncode`). That library
only accepts data that starts with a 0x580-byte DNAS install header:

- **The header holds RSA-1024 signatures.** Its records are checked with
  public keys stored in `dnas_ins` (the first with key 83). The matching
  private keys belong to Sony and are not on the client, so nobody outside
  Sony can sign a new header.
- **A raw blob shorter than 0x580 bytes** (like the current ones) makes the
  client wait forever: the DNAS install bar stops at 100%.
- **A raw blob of 0x580 bytes or more** fails the signature check and the
  client shows a DNAS error.

This is not a DNAS server problem. The DNAS authentication before the install
succeeds; the client fails afterwards, on the data itself. Both cases were
reproduced by running the clients' own code in an emulator (EU and NA). The
addresses are in `opensnap_app/capcom/monsterhunter/patch.py` and
`tools/dnas/install_packet.py`.

Resident Evil Outbreak uses the same library with the same keys, so it has the
same limit.

## Wrapping a patch with a signed header

`tools/dnas/install_packet.py` builds `[0x580 header][payload]` from a
genuine signed header, for example one taken from a retail install download:

    python3 -m tools.dnas.install_packet --overlay dnas_ins.dec.bin \
        --header header.bin --payload eu_custom_1002.bin -o eu_custom_1002.signed.bin

`--overlay` is the title's `dnas_ins` overlay, decrypted first (the overlay
decrypts its own code at run time; the scheme is described in
`tools/dnas/install_packet.py`). `--inspect` prints what the tool reads from
it. The tool cannot generate a header: without `--header` it stops with an
error.

Two open points before this can work:

- **No genuine header is available.** No retail Monster Hunter install
  download has been captured.
- **A header may not carry a different payload.** The install also checks
  more RSA signatures while it processes the payload. If they cover the
  payload itself, a genuine header only works with the payload it was signed
  with.

Until both are settled, keep these entries disabled. A patch only works
on these clients if the client itself is patched to skip the DNAS install
step.
