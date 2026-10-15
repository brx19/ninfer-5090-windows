"""The Winsock dependency of the media acquisition layer, as a CMake target-graph fact.

Why this gate exists: the Windows build linked ninfer.exe through ninfer_media_acquire, whose
objects call WSAStartup, getaddrinfo, freeaddrinfo, ntohl and inet_ntop; the target never linked
ws2_32, so the link failed with five LNK2019 unresolved externals
(brx19/ninfer-5090-windows run 38700892272). The fix is a target-scoped PRIVATE link dependency,
which a static library propagates to every consumer's final link line -- so this test asserts the
dependency where it must exist, and that no new Winsock user can appear in a target that does not
receive it, which would reintroduce the failure on the next synchronized candidate.

The full link itself is exercised by the release workflow on a Windows runner; this test is the
cheap, host-independent guard that the target graph stays in the state the link requires.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PRODUCT_CMAKE = REPO / "src" / "product" / "CMakeLists.txt"
ACQUIRE_CPP = REPO / "src" / "product" / "media_acquire" / "acquire.cpp"
APPS_CMAKE = REPO / "apps" / "CMakeLists.txt"
SERVE_CMAKE = REPO / "src" / "serve" / "CMakeLists.txt"


def winsock_includes(path: Path) -> bool:
    return "<winsock2.h>" in path.read_text(encoding="utf-8", errors="replace").lower()


def test_acquire_uses_winsock_and_the_target_links_it() -> None:
    # The code side of the contract: if this include disappears, the ws2_32 link is dead weight
    # and should be removed with it -- this assertion is what forces that pairing to be noticed.
    assert winsock_includes(ACQUIRE_CPP)
    text = PRODUCT_CMAKE.read_text(encoding="utf-8")
    block = re.search(
        r"if\(WIN32\)\s*\n(.*?)\nendif\(\)", text, re.DOTALL
    )
    assert block is not None, "no WIN32 block in src/product/CMakeLists.txt"
    assert re.search(
        r"target_link_libraries\(\s*ninfer_media_acquire\s+PRIVATE\s+ws2_32\s*\)",
        block.group(1),
    ), "ninfer_media_acquire must link ws2_32 under WIN32"


def test_the_winsock_consumers_all_receive_the_dependency() -> None:
    # Every file that includes Winsock must sit in a target that receives ws2_32 -- directly
    # or transitively through ninfer_media_acquire, whose private dependency reaches each
    # consumer's final link line. A new include outside this set is a future LNK2019 waiting
    # for its next build; asserting the set (not individual files) is what forces the
    # re-derivation when that happens.
    users = {
        p.relative_to(REPO).as_posix()
        for source in ("src", "apps")
        for p in (REPO / source).rglob("*.cpp")
        if winsock_includes(p)
    }
    # apps/serve/main.cpp is covered too: it comments on the winsock2.h ordering rather than
    # including it, and its WSA usage resolves through ninfer_serve, which links
    # ninfer_media_acquire. If that propagation changes, this assertion's detail will say so.
    assert users == {
        "src/product/media_acquire/acquire.cpp",
        "src/serve/http_transport.cpp",
    }, f"Winsock usage set changed: {sorted(users)}; re-derive its link dependencies"

    serve = SERVE_CMAKE.read_text(encoding="utf-8")
    assert "ninfer_media_acquire" in serve, "ninfer_serve no longer links the media acquisition layer"
    apps = APPS_CMAKE.read_text(encoding="utf-8")
    assert "ninfer_product_prompt_input" in apps, "ninfer no longer links the product prompt input"
    assert "ninfer_serve" in apps, "ninfer-serve no longer links the serve layer"
