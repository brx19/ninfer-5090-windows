#ifdef _WIN32

#include "core/io/read_queue.h"
#include "core/io/file.h"
#include "core/memory.h"

#include <algorithm>
#include <condition_variable>
#include <mutex>
#include <system_error>
#include <vector>
#include <io.h>
#include <fcntl.h>

namespace ninfer::io {
namespace {

struct ReadRequest { DirectRead request; };

} // namespace

struct DirectReadQueue::Impl {
    std::vector<FileView> files;
    std::vector<MemorySpan> buffers;
    std::uint32_t capacity = 0;
    bool closed = false;
    bool signaled = false;

    std::vector<DirectRead> admitted;
    std::vector<ReadCompletion> completed;

    mutable std::mutex mutex;
    std::condition_variable cv;

    std::size_t inflight() const { return admitted.size() + completed.size(); }

    void execute(const DirectRead& r) {
        ReadCompletion c{r.tag, r.buffer_index, 0, 0};
        if (r.file_index >= files.size() || r.buffer_index >= buffers.size()) {
            c.error = EINVAL;
            completed.push_back(c);
            return;
        }
        const auto& fv = files[r.file_index];
        if (fv.descriptor < 0) { c.error = EBADF; completed.push_back(c); return; }
        auto* dst = buffers[r.buffer_index].data;
        if (r.bytes > 0 && dst == nullptr) { completed.push_back(c); return; }
        std::size_t done = 0;
        while (done < r.bytes) {
            _lseeki64(fv.descriptor, (long long)(r.file_offset + done), SEEK_SET);
            unsigned int chunk = (unsigned int)std::min<std::size_t>(r.bytes - done, 64u * 1024 * 1024);
            if (chunk == 0) break;
            int n = _read(fv.descriptor, static_cast<char*>(dst) + done, chunk);
            if (n < 0) { c.error = errno ? errno : EIO; break; }
            done += static_cast<std::size_t>(n);
            // A short read on a regular file means EOF. Stop here: with FILE_FLAG_NO_BUFFERING the
            // next lseek would land on a non-sector-aligned offset and fail with EINVAL. The
            // io_uring reference issues a single positioned read, so it never retries past EOF.
            if (static_cast<std::size_t>(n) < chunk) break;
        }
        c.bytes = static_cast<std::uint32_t>(done);
        // A short read (clean EOF before r.bytes, common for the final 4096-aligned window of a
        // non-multiple-of-4096 file) is a success: ReadCompletion documents that a successful read
        // may be short. The io_uring reference only sets error for a negative (failed) result. The
        // caller validates bytes against its required count.
        completed.push_back(c);
    }
};

DirectReadQueue::DirectReadQueue(std::span<const FileView> files, std::span<const MemorySpan> buffers,
                                std::uint32_t capacity)
    : impl_(std::make_unique<Impl>()) {
    impl_->files.assign(files.begin(), files.end());
    impl_->buffers.assign(buffers.begin(), buffers.end());
    impl_->capacity = capacity ? capacity : 1;
}

DirectReadQueue::~DirectReadQueue() { drain(); }

ReadAdmission DirectReadQueue::try_enqueue(DirectRead request) {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    if (impl_->closed) return ReadAdmission::Closed;
    if (impl_->inflight() >= impl_->capacity) return ReadAdmission::Full;
    impl_->admitted.push_back(request);
    return ReadAdmission::Accepted;
}

void DirectReadQueue::submit() {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    for (auto& r : impl_->admitted) impl_->execute(r);
    impl_->admitted.clear();
}

std::size_t DirectReadQueue::poll(std::span<ReadCompletion> output) {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    std::size_t n = std::min<std::size_t>(output.size(), impl_->completed.size());
    for (std::size_t i = 0; i < n; ++i) output[i] = impl_->completed[i];
    if (n > 0) impl_->completed.erase(impl_->completed.begin(), impl_->completed.begin() + n);
    return n;
}

ReadWake DirectReadQueue::wait() {
    std::unique_lock<std::mutex> lk(impl_->mutex);
    impl_->cv.wait(lk, [&]{ return !impl_->completed.empty() || impl_->closed || impl_->signaled; });
    if (impl_->signaled) { impl_->signaled = false; return ReadWake::Signalled; }
    return ReadWake::Completion;
}

void DirectReadQueue::wake() {
    { std::lock_guard<std::mutex> lk(impl_->mutex); impl_->signaled = true; }
    impl_->cv.notify_all();
}

void DirectReadQueue::close() {
    { std::lock_guard<std::mutex> lk(impl_->mutex); impl_->closed = true; }
    submit();
    impl_->cv.notify_all();
}

void DirectReadQueue::drain() {
    close();
    std::lock_guard<std::mutex> lk(impl_->mutex);
    impl_->completed.clear();
    impl_->admitted.clear();
}

std::size_t DirectReadQueue::outstanding() const noexcept {
    std::lock_guard<std::mutex> lk(impl_->mutex);
    return impl_->inflight();
}

} // namespace ninfer::io

#else

// (original upstream implementation retained below)

#include "core/io/read_queue.h"

#include <liburing.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <exception>
#include <limits>
#include <stdexcept>
#include <system_error>
#include <vector>

#include <poll.h>
#include <sys/eventfd.h>
#include <unistd.h>

namespace ninfer::io {
namespace {

void check(int result, const char* operation) {
    if (result < 0) throw std::system_error(-result, std::generic_category(), operation);
}

} // namespace

struct DirectReadQueue::Impl {
    struct Slot {
        DirectRead request{};
        bool busy = false;
    };

    io_uring ring{};
    bool initialized       = false;
    bool closed            = false;
    int notification       = -1;
    std::uint32_t capacity = 0;
    std::size_t pending    = 0;
    std::vector<FileView> files;
    std::vector<MemorySpan> buffers;
    std::vector<Slot> slots;
    std::vector<ReadCompletion> completions;
    std::vector<bool> slot_busy;

    std::size_t inflight() const { return pending; }
};

DirectReadQueue::DirectReadQueue(std::span<const FileView> files, std::span<const MemorySpan> buffers,
                                std::uint32_t capacity)
    : impl_(std::make_unique<Impl>()) {
    impl_->files.assign(files.begin(), files.end());
    impl_->buffers.assign(buffers.begin(), buffers.end());
    impl_->capacity = capacity ? capacity : 1;
    impl_->notification = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
    if (impl_->notification < 0) throw std::system_error(errno, std::generic_category(), "eventfd");
    io_uring_params params{};
    if (io_uring_queue_init(impl_->capacity + 1, &impl_->ring, &params) < 0)
        throw std::system_error(errno, std::generic_category(), "io_uring_queue_init");
    impl_->initialized = true;
    impl_->slots.resize(impl_->capacity);
    for (auto& s : impl_->slots) s.busy = false;
}

DirectReadQueue::~DirectReadQueue() { drain(); }

ReadAdmission DirectReadQueue::try_enqueue(DirectRead request) {
    if (impl_->closed) return ReadAdmission::Closed;
    for (std::size_t i = 0; i < impl_->slots.size(); ++i) {
        if (!impl_->slots[i].busy) {
            impl_->slots[i].request = request;
            impl_->slots[i].busy = true;
            ++impl_->pending;
            return ReadAdmission::Accepted;
        }
    }
    return ReadAdmission::Full;
}

void DirectReadQueue::submit() {
    for (std::size_t i = 0; i < impl_->slots.size(); ++i) {
        if (!impl_->slots[i].busy) continue;
        const auto& r = impl_->slots[i].request;
        io_uring_sqe* sqe = io_uring_get_sqe(&impl_->ring);
        if (!sqe) continue;
        io_uring_prep_read(sqe, impl_->files[r.file_index].descriptor,
                           impl_->buffers[r.buffer_index].data, r.bytes,
                           (off_t)r.file_offset);
        io_uring_sqe_set_data(sqe, reinterpret_cast<void*>(i));
        io_uring_submit(&impl_->ring);
    }
}

std::size_t DirectReadQueue::poll(std::span<ReadCompletion> output) {
    io_uring_cqe* cqe = nullptr;
    unsigned head = 0;
    unsigned count = 0;
    io_uring_for_each_cqe(&impl_->ring, head, cqe) {
        if (count >= output.size()) break;
        std::size_t idx = reinterpret_cast<std::size_t>(io_uring_cqe_get_data(cqe));
        ReadCompletion rc{};
        rc.tag = impl_->slots[idx].request.tag;
        rc.buffer_index = impl_->slots[idx].request.buffer_index;
        rc.bytes = cqe->res > 0 ? (std::uint32_t)cqe->res : 0;
        rc.error = cqe->res < 0 ? -cqe->res : 0;
        output[count++] = rc;
        impl_->slots[idx].busy = false;
        if (impl_->pending > 0) --impl_->pending;
    }
    if (count) io_uring_cq_advance(&impl_->ring, count);
    return count;
}

ReadWake DirectReadQueue::wait() {
    struct pollfd pfd { impl_->notification, POLLIN, 0 };
    poll(&pfd, 1, -1);
    return ReadWake::Signalled;
}

void DirectReadQueue::wake() {
    uint64_t one = 1;
    (void)write(impl_->notification, &one, sizeof(one));
}

void DirectReadQueue::close() {
    impl_->closed = true;
    wake();
}

void DirectReadQueue::drain() {
    close();
    while (impl_->pending > 0) {
        std::array<ReadCompletion, 64> buf{};
        (void)poll(std::span<ReadCompletion>(buf));
    }
    if (impl_->notification >= 0) { close(impl_->notification); impl_->notification = -1; }
    if (impl_->initialized) { io_uring_queue_exit(&impl_->ring); impl_->initialized = false; }
}

std::size_t DirectReadQueue::outstanding() const noexcept {
    return impl_->inflight();
}

} // namespace ninfer::io

#endif
