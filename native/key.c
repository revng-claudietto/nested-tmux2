/* Local keyboard RPC client. No tmux parsing, shell execution, or SSH here. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#ifndef NT_KEY_TIMEOUT_MS
#define NT_KEY_TIMEOUT_MS 30000
#endif

static int fail(const char *message) {
    fprintf(stderr, "nested-tmux-key: %s\n", message);
    return 1;
}

static int64_t now_ms(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
        return -1;
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int ready(int fd, short events, int64_t deadline) {
    for (;;) {
        int64_t now = now_ms();
        if (now < 0)
            return -1;
        if (now >= deadline) {
            errno = ETIMEDOUT;
            return -1;
        }
        struct pollfd item = {.fd = fd, .events = events};
        int result = poll(&item, 1, (int)(deadline - now));
        if (result > 0)
            return 0; /* recv/send also handle EOF, hangup, and socket errors. */
        if (result < 0 && errno != EINTR)
            return -1;
    }
}

static int exchange(int fd, const char *action, int64_t deadline) {
    char request[80];
    int length = snprintf(request, sizeof(request),
                          "{\"op\":\"key\",\"action\":\"%s\"}\n", action);
    if (length < 0 || (size_t)length >= sizeof(request))
        return fail("request too large");
    size_t sent = 0;
    while (sent < (size_t)length) {
        if (ready(fd, POLLOUT, deadline) < 0)
            return fail(strerror(errno));
        ssize_t count = send(fd, request + sent, (size_t)length - sent, MSG_NOSIGNAL);
        if (count < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
                continue;
            return fail(strerror(errno));
        }
        if (count == 0)
            return fail("connection closed while sending request");
        sent += (size_t)count;
    }

    char response[4096];
    size_t used = 0;
    while (used < sizeof(response)) {
        if (ready(fd, POLLIN, deadline) < 0)
            return fail("no acknowledgement; action outcome is unknown (not retried)");
        ssize_t count = recv(fd, response + used, sizeof(response) - used, 0);
        if (count < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
                continue;
            return fail("connection failed; action outcome is unknown (not retried)");
        }
        if (count == 0)
            return fail("connection closed without acknowledgement; action outcome is unknown");
        used += (size_t)count;
        char *newline = memchr(response, '\n', used);
        if (!newline)
            continue;
        if (newline != response + used - 1)
            return fail("invalid controller response");
        if (used == 3 && memcmp(response, "OK\n", 3) == 0)
            return 0;
        if (used > 5 && memcmp(response, "ERR ", 4) == 0) {
            /* Diagnostics can originate remotely. Never print terminal controls. */
            for (size_t i = 4; i < used - 1; ++i) {
                unsigned char c = (unsigned char)response[i];
                if (c < 32 || c > 126)
                    response[i] = '?';
            }
            response[used - 1] = '\0';
            return fail(response + 4);
        }
        return fail("invalid controller response");
    }
    return fail("controller response exceeds size limit");
}

int main(int argc, char **argv) {
    if (argc != 3)
        return fail("usage: nested-tmux-key SOCKET ACTION");
    const char *actions[] = {"left", "right", "up", "down", "last", "new", "copy", "remove",
                            "select-0", "select-1", "select-2", "select-3", "select-4",
                            "select-5", "select-6", "select-7", "select-8", "select-9"};
    size_t i;
    for (i = 0; i < sizeof(actions) / sizeof(actions[0]); ++i)
        if (strcmp(argv[2], actions[i]) == 0)
            break;
    if (i == sizeof(actions) / sizeof(actions[0]))
        return fail("unknown keyboard action");
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(argv[1]) >= sizeof(address.sun_path))
        return fail("socket path is too long");
    strcpy(address.sun_path, argv[1]);
    int64_t start = now_ms();
    if (start < 0)
        return fail(strerror(errno));
    int64_t deadline = start + NT_KEY_TIMEOUT_MS;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return fail(strerror(errno));
    int result = 1;
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        if (errno != EINPROGRESS || ready(fd, POLLOUT, deadline) < 0) {
            fail(strerror(errno));
            goto done;
        }
        int error = 0;
        socklen_t size = sizeof(error);
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) < 0 || error) {
            fail(strerror(error ? error : errno));
            goto done;
        }
    }
    result = exchange(fd, argv[2], deadline);
done:
    close(fd);
    return result;
}
