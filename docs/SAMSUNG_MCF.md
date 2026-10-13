# Why MCF / "Second Screen" one-tap is not cloned

REA inventories of the Store app show Samsung **MCF** (Multi Connection
Framework) plus Android packages `com.samsung.android.secondscreen` and
`com.samsung.android.smartmirroring`. That path does discovery, advertisement,
and handshake before media starts.

MCF is proprietary. Replicating it would need reverse engineering beyond
clean-room metadata, possible cloud/account keys, and would risk trademark and
ToS issues. This project therefore implements the **open Miracast / Wi-Fi
Display source** path that the same Windows app also prepares
(`PrepareMiracastConnection`).

If your Galaxy Tab only offers the Samsung "Second Screen" tile and refuses
generic wireless display sinks, interoperability may fail until Samsung opens
MCF or a third party documents a safe handshake. Use Smart View / screen
mirroring (WFD sink) when available.
