/* Runs the real editor/control/decoder functions with mocked hardware.
 * Compile with -Itests/host -fsanitize=address,undefined -ffunction-sections
 * and -Wl,--gc-sections. This is not a TI hardware emulator.
 */
#include <assert.h>
#define main calculator_main
#include "../calculator/src/main.c"
#undef main

uint8_t gfx_vbuffer[240][320];
uint16_t gfx_palette[256];
uint8_t kb_Data[8];
static char sent[4096];
static size_t sent_count;
static int usb_status;
void kb_Scan(void) {}
int usb_HandleEvents(void) { return usb_status; }
int srl_Write(void *dev, const void *buf, size_t size) {
    (void)dev;
    assert(sent_count + size < sizeof(sent));
    memcpy(sent + sent_count, buf, size);
    sent_count += size;
    return (int)size;
}
int srl_Read(void *d, void *b, size_t s) { (void)d; (void)b; (void)s; return 0; }
int srl_UsbEventCallback(int e, void *d, void *c) { (void)e; (void)d; (void)c; return 0; }
static void key(int group, uint8_t bit, bool shift) {
    memset(event_keys, 0, sizeof(event_keys));
    event_keys[group] = bit; event_shift = shift;
    if (typing) input_keys(); else remote_keys();
}

int main(void) {
    unsigned i, pos;
    uint8_t strip[48][320];
    memset(gfx_vbuffer, 77, sizeof(gfx_vbuffer));

    key(2, kb_Alpha, false);
    assert(typing && edit_len == 0);
    /* The previous harness used a fake font, masking the blank panel bug. */
    unsigned white = 0;
    for (unsigned y = OVERLAY_Y + 2; y < OVERLAY_Y + 10; ++y)
        for (unsigned x = 3; x < 315; ++x)
            white += gfx_vbuffer[y][x] == 255;
    assert(white > 100);
    assert(editor_font['A' - 32][0] != 0);
    key(2, kb_Math, false); /* a */
    key(3, kb_Apps, false); /* b */
    key(7, kb_Left, false);
    key(4, kb_Prgm, false); /* insert c before b */
    assert(!strcmp(edit_text, "acb"));
    key(1, kb_Del, false);
    assert(!strcmp(edit_text, "ab"));
    if (getenv("MAC84_EDITOR_DUMP")) {
        FILE *f = fopen(getenv("MAC84_EDITOR_DUMP"), "wb");
        assert(f); fwrite(gfx_vbuffer, 1, sizeof(gfx_vbuffer), f); fclose(f);
    }
    key(6, kb_Clear, false);
    assert(edit_len == 0 && edit_pos == 0);
    for (i = 0; i < 110; i++) input_insert('w');
    assert(edit_len == 100);
    for (pos = 0; pos <= 100; pos++) {
        edit_pos = pos;
        input_redraw(); /* Sanitizers check every scroll/caret position. */
    }
    memcpy(strip, gfx_vbuffer + 192, sizeof(strip));
    memset(packet, 42, 256);
    assert(draw_tile(0, 12, 'P', 256));
    assert(!memcmp(strip, gfx_vbuffer + 192, sizeof(strip)));
    assert(draw_tile(0, 0, 'P', 256));
    assert(gfx_vbuffer[0][0] == 42);
    packet[0] = 255; packet[1] = 44; packet[2] = 1; packet[3] = 44;
    assert(draw_tile(1, 12, 'R', 4));
    assert(!memcmp(strip, gfx_vbuffer + 192, sizeof(strip)));
    key(6, kb_Enter, false);
    assert(!typing && force_refresh);
    assert(sent_count == 105); /* K| + 100 chars + newline, I + newline */
    assert(sent[103] == 'I');
    key(2, kb_Alpha, false);
    key(1, kb_Mode, false);
    assert(!typing && !quit_requested);
    size_t before_spaces = sent_count;
    key(5, kb_Vars, false);
    key(6, kb_Clear, false);
    assert(sent_count == before_spaces + 8);
    assert(!memcmp(sent + before_spaces, "S|L\nS|R\n", 8));
    /* v0.3.4 top row: ZOOM double click, Y= look, WINDOW / 2ND+WINDOW. */
    {
        size_t before_top = sent_count;
        const char *expect = "C|D|160|120\nL|160|120\nX|M\nX|A\n";
        key(1, kb_Zoom, false);
        key(1, kb_Yequ, false);
        key(1, kb_Window, false);
        key(1, kb_Window, true);
        assert(sent_count - before_top == strlen(expect));
        assert(!memcmp(sent + before_top, expect, strlen(expect)));
    }
    /* Packed tile decode: 3 colors at 2 bits; out-of-range index rejected. */
    {
        int i;
        memset(packet, 0, sizeof(packet));
        packet[0] = 3; packet[1] = 10; packet[2] = 20; packet[3] = 30;
        for (i = 0; i < 64; ++i) packet[4 + i] = 0x24; /* 00 10 01 00 */
        typing = false;
        assert(draw_tile(2, 3, 'Q', 68));
        assert(gfx_vbuffer[48][32] == 10 && gfx_vbuffer[48][33] == 30 &&
               gfx_vbuffer[48][34] == 20 && gfx_vbuffer[63][47] == 10);
        packet[4] = 0xC0; /* index 3 with only 3 colors */
        assert(!draw_tile(2, 3, 'Q', 68));
        assert(!draw_tile(2, 3, 'Q', 67)); /* wrong length */
        assert(tile_header("T|19|14|Q|145", &i, &i, (char *)packet, &i));
        assert(!tile_header("T|19|14|Q|146", &i, &i, (char *)packet, &i));
    }
    key(2, kb_Alpha, false);
    /* Sequential 2ND, release, MODE is accepted, not only a held chord. */
    memset(prev_keys, 0, sizeof(prev_keys));
    kb_Data[1] = kb_2nd;
    last_scan = clock() - CLOCKS_PER_SEC;
    scan_controls();
    kb_Data[1] = 0;
    last_scan = clock() - CLOCKS_PER_SEC;
    scan_controls();
    kb_Data[1] = kb_Mode;
    last_scan = clock() - CLOCKS_PER_SEC;
    scan_controls();
    assert(quit_requested); /* Emergency quit also works while typing. */
    quit_requested = false;
    memset(kb_Data, 0, sizeof(kb_Data));
    /* v0.3.3: a non-success usb_HandleEvents() code (e.g. USB_IGNORE = 1 from
       srldrvce) is counted and reported, not treated as a lost link. */
    link_open = true; usb_status = 1;
    assert(send_line("test\n"));
    assert(!link_lost && usb_event_errors > 0 && last_usb_error == 1);
    usb_status = 0;
    /* A disconnect of OUR device is fatal and records a reason. */
    fail_reason[0] = 0;
    link_device = (usb_device_t)&sent;
    usb_callback(USB_DEVICE_DISCONNECTED_EVENT, (void *)&sent_count, NULL);
    assert(!link_lost); /* some other device */
    usb_callback(USB_DEVICE_DISCONNECTED_EVENT, (void *)&sent, NULL);
    assert(link_lost && strstr(fail_reason, "disconnect"));
    assert(!send_line("test\n"));
    /* The first reason is kept; later consequences do not overwrite it. */
    assert(strstr(fail_reason, "disconnect"));
    puts("Calculator editor, tile overlay, emergency exit: PASS");
    return 0;
}
