// Regression test for the Windows Direct I/O path (issue #7, fixed 2026-10-15).
//
// The 2026-10-14 upstream merge (8777393d) replaced the Windows weight-load read path with a
// CRT _wsopen_s/_read handle, so ReadMode::Direct silently became buffered page I/O with
// alignment 1 and weight load dropped from about 3.9 to about 2.1 GiB/s. This test locks the
// restored contract:
//
//   1. On Windows, Direct mode reports the 4096 memory/offset alignment that the
//      FILE_FLAG_NO_BUFFERING handle requires. The pre-fix CRT path reported 1/1.
//   2. A sequential full-file read at the materializer's 64 MiB chunk pattern stays above a
//      measured floor on the release machine's NVMe.
//
// Measured 2026-10-15 on the release machine (Windows 11 x64, RTX 5090, local NVMe), 17.65 GiB
// artifact, 64 MiB aligned chunks: pre-fix CRT path 2.51-2.58 GiB/s; post-fix NO_BUFFERING
// path 5.48-5.60 GiB/s. The 3.5 GiB/s floor sits between the two with a >30 % margin on both.
//
// Skips with 77 when NINFER_TEST_ARTIFACT is unset or points at a file under 4 GiB, where the
// timing assertion is noise.

#include "core/io/file.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <span>
#include <stdexcept>
#include <string>

#ifdef _WIN32
#include <malloc.h>
#define DIRECT_ALLOC(bytes) _aligned_malloc(bytes, 4096)
#define DIRECT_FREE(pointer) _aligned_free(pointer)
#else
#define DIRECT_ALLOC(bytes) std::aligned_alloc(4096, bytes)
#define DIRECT_FREE(pointer) std::free(pointer)
#endif

namespace {

void require(bool condition, const char* message) {
    if (!condition) { throw std::runtime_error(message); }
}

constexpr std::size_t kChunkBytes     = 64ULL * 1024 * 1024; // The materializer's kReadBlockBytes.
constexpr double kFloorGiBps          = 3.5;
constexpr std::uint64_t kMinimumBytes = 4ULL * 1024 * 1024 * 1024;

} // namespace

int main() {
    const char* artifact = std::getenv("NINFER_TEST_ARTIFACT");
    if (!artifact || !*artifact) {
        std::cout << "skip: NINFER_TEST_ARTIFACT is not set\n";
        return 77;
    }
    try {
        ninfer::io::ReadOnlyFile input(artifact, ninfer::io::ReadMode::Direct);
        const auto view = input.view();
        if (view.bytes < kMinimumBytes) {
            std::cout << "skip: artifact is smaller than the 4 GiB timing floor\n";
            return 77;
        }
#ifdef _WIN32
        require(view.memory_alignment == 4096 && view.offset_alignment == 4096,
                "Direct mode lost its 4096-alignment contract (buffered CRT fallback)");
#endif
        void* buffer = DIRECT_ALLOC(kChunkBytes + 4096);
        require(buffer != nullptr, "direct read buffer allocation failed");
        std::uint64_t off = 0, received = 0;
        const auto t0 = std::chrono::steady_clock::now();
        while (off < view.bytes) {
            const auto want = static_cast<std::size_t>(
                std::min<std::uint64_t>(kChunkBytes, view.bytes - off));
            const auto window = ninfer::io::direct_read_window(view, off, want);
            require(window.offset % view.offset_alignment == 0 &&
                        window.bytes % view.offset_alignment == 0,
                    "direct read window left the aligned contract");
            const auto n = input.read_direct(window.offset,
                                             {static_cast<std::byte*>(buffer), window.bytes});
            require(n >= window.required, "direct read ended before the required payload");
            received += window.required;
            off = window.offset + window.required;
        }
        const auto t1 = std::chrono::steady_clock::now();
        DIRECT_FREE(buffer);
        const double seconds = std::chrono::duration<double>(t1 - t0).count();
        const double gibps   = received / 1073741824.0 / seconds;
        std::cout << "direct io: " << received / 1073741824.0 << " GiB in " << seconds
                  << " s = " << gibps << " GiB/s (floor " << kFloorGiBps << " GiB/s)\n";
        require(gibps >= kFloorGiBps, "direct read throughput fell below the regression floor");
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "FAIL: " << e.what() << "\n";
        return 1;
    }
}
