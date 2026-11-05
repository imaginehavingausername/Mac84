/* Actual calculator frame parser over POSIX pipes with deliberately partial I/O.
 * Hardware calls are mocked; this is a transport integration test, not an emulator.
 */
#include <assert.h>
#include <unistd.h>
#include <poll.h>
#define main calculator_main
#include "../calculator/src/main.c"
#undef main
uint8_t gfx_vbuffer[240][320];
uint16_t gfx_palette[256];
uint8_t kb_Data[8];
void kb_Scan(void) {}
/* Real hardware may return non-success callback codes (e.g. USB_IGNORE = 1).
   v0.3.3 must keep transferring and report them between batches. */
static unsigned long usb_calls;
int usb_HandleEvents(void) { return (++usb_calls % 4000) == 0 ? 1 : USB_SUCCESS; }
int srl_Write(void *dev, const void *buf, size_t size) {
    (void)dev;
    return (int)write(STDOUT_FILENO, buf, size > 3 ? 3 : size);
}
int srl_Read(void *dev, void *buf, size_t size) {
    struct pollfd p = {STDIN_FILENO, POLLIN, 0};
    (void)dev;
    int n = poll(&p, 1, 0);
    if (n < 0 || (p.revents & (POLLERR | POLLHUP))) return -1;
    if (!n) return 0;
    return (int)read(STDIN_FILENO, buf, size > 7 ? 7 : size);
}
int main(int argc, char **argv) {
    char greeting[50];
    assert(argc == 2);
    link_open = true;
    assert(send_line("@M84|5\n"));
    assert(recv_line(greeting, sizeof(greeting), 8));
    assert(!strcmp(greeting, "@M84READY|5"));  /* packed tiles negotiated */
    online = true;
    for (int i = 0; i < 11; i++) assert(fetch_frame());
    cursor_hide();
    FILE *out = fopen(argv[1], "wb");
    assert(out);
    assert(fwrite(gfx_vbuffer, 1, sizeof(gfx_vbuffer), out) == sizeof(gfx_vbuffer));
    fclose(out);
    {
        char done[40];
        snprintf(done, sizeof(done), "DONE|%d|%d\n", cursor_x, cursor_y);
        assert(send_line(done));  /* reports where M| put the cursor */
    }
    return 0;
}
