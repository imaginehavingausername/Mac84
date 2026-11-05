/*
 * MAC84 v0.3.4 — TI-84 Plus CE native USB desktop viewer.
 * Mac -> TI: F|n|more\n, followed by T|x|y|P/R|len\n + len binary bytes per tile, E\n.
 * TI -> Mac: V\n requests up to 36 dirty 16x16 tiles.
 * The TI returns A\n after each group of four tiles or the final shorter group.
 * v0.3 grouped-ACK transport; startup uses @M84 / @M84READY.
 */
#include <graphx.h>
#include <keypadc.h>
#include <usbdrvce.h>
#include <srldrvce.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define SCREEN_W 320
#define SCREEN_H 240
#define TILE 16
#define ACK_GROUP 4
#define MAX_TILE_PACKET 512
#define OVERLAY_Y 192
#define OVERLAY_H 48

static srl_device_t srl;
static uint8_t serial_buffer[512];
static uint8_t packet[MAX_TILE_PACKET];
#define KEY_QUEUE 48
static struct KeyEvent { uint8_t group, bit; bool shifted; } key_queue[KEY_QUEUE];
static uint8_t prev_keys[8], event_keys[8];
static uint8_t queue_head = 0, queue_tail = 0;
static bool event_shift = false;
static bool second_latched = false;
static bool quit_requested = false;
static bool link_lost = false;
static bool link_open = false;
static uint8_t zoom_level = 1;
static bool next_batch = false;
static clock_t last_scan, next_repeat;
static void scan_controls(void);
static void process_controls(void);
static void input_keys(void);
static void input_redraw(void);
static void remote_keys(void);
static uint8_t cursor_bg[81];
#include "editor_font.h"
static char edit_text[101];
static uint8_t edit_len = 0, edit_pos = 0, edit_mode = 0;
static bool typing = false;
static bool cursor_visible = false;
static bool online = false;
static int cursor_x = 160, cursor_y = 120;
static bool force_refresh = false;

/* v0.3.3 diagnostics: every stop records WHY, so the calculator never exits
   silently. Only the first reason after a reset is kept (it is the cause;
   later failures are usually consequences). */
static usb_device_t link_device = NULL;
static char fail_reason[40];
static char bad_line[26];
static const char *stage = "starting";
static unsigned batch_number = 0;
static int stage_tile = 0, stage_total = 0;
static unsigned usb_event_errors = 0, usb_errors_reported = 0;
static int last_usb_error = 0;

static void fail(const char *reason) {
    if (!fail_reason[0]) {
        strncpy(fail_reason, reason, sizeof(fail_reason) - 1);
        fail_reason[sizeof(fail_reason) - 1] = 0;
    }
}

static void fail_code(const char *what, int code) {
    char text[40];
    snprintf(text, sizeof(text), "%s %d", what, code);
    fail(text);
}

static void note_bad_line(const char *line) {
    size_t i;
    for (i = 0; line[i] && i + 1 < sizeof(bad_line); ++i)
        bad_line[i] = (line[i] >= 32 && line[i] < 127) ? line[i] : '?';
    bad_line[i] = 0;
}

static usb_error_t usb_callback(usb_event_t event, void *data,
                                 usb_callback_data_t *callback) {
    /* Same rule as Gemini84: only OUR serial device going away is fatal. */
    if (event == USB_DEVICE_DISCONNECTED_EVENT && link_open &&
        (link_device == NULL || data == link_device)) {
        link_lost = true;
        fail("USB disconnect event from Mac");
    }
    return srl_UsbEventCallback(event, data, callback);
}

/* usb_HandleEvents() returns whatever a callback returned. srldrvce may
   legitimately return non-success codes (e.g. USB_IGNORE = 1 after it answers
   a CDC setup request). CEdev's srl_echo example and Gemini84 ignore this value
   and detect real failures through negative srl_Read/srl_Write results.
   v0.3.2 treated it as a lost link and quit silently. Count it and report it
   to the Mac instead; a dead link still fails through srl_Read/srl_Write. */
static void usb_poll(void) {
    usb_error_t err = usb_HandleEvents();
    if (err != USB_SUCCESS) {
        ++usb_event_errors;
        last_usb_error = (int)err;
    }
}

/* srldrvce uses nonblocking buffering: ALWAYS retry partial writes. */
static bool send_bytes(const void *buf, size_t size) {
    size_t sent = 0;
    clock_t start = clock();
    while (sent < size) {
        int written;
        usb_poll();
        scan_controls();
        if (quit_requested) return false;
        if (link_lost) { fail("USB link lost while writing"); return false; }
        written = srl_Write(&srl, (const uint8_t *)buf + sent, size - sent);
        if (written < 0) { fail_code("srl_Write error", written); return false; }
        sent += (size_t)written;
        if (clock() - start > (clock_t)(8 * CLOCKS_PER_SEC)) {
            fail("USB write timed out (8 s)");
            return false;
        }
    }
    return true;
}

static bool send_line(const char *line) {
    return send_bytes(line, strlen(line));
}

/* v0.3.4: small receive buffer. Lines and binary payloads are both served
   from it, so nothing is "swallowed" between a header and its packet, and a
   header byte no longer costs a USB poll + key scan + 1-byte srl_Read. */
static uint8_t rx_buf[128];
static uint8_t rx_pos = 0, rx_len = 0;

static bool rx_refill(clock_t deadline) {
    while (clock() < deadline) {
        int n;
        usb_poll();
        scan_controls();
        if (quit_requested) return false;
        if (link_lost) { fail("USB link lost while reading"); return false; }
        n = srl_Read(&srl, rx_buf, sizeof(rx_buf));
        if (n > 0) { rx_pos = 0; rx_len = (uint8_t)n; return true; }
        if (n < 0) { fail_code("srl_Read error", n); return false; }
    }
    return false;
}

static bool recv_byte(uint8_t *result, clock_t deadline) {
    if (rx_pos >= rx_len && !rx_refill(deadline)) return false;
    *result = rx_buf[rx_pos++];
    return true;
}

static bool recv_line(char *out, size_t capacity, int seconds) {
    clock_t end = clock() + (clock_t)seconds * CLOCKS_PER_SEC;
    size_t n = 0;
    while (true) {
        uint8_t c;
        if (!recv_byte(&c, end)) {
            if (!quit_requested && !link_lost && clock() >= end)
                fail("Timed out waiting for Mac line");
            return false;
        }
        if (c == '\n') {
            out[n] = 0;
            return true;
        }
        if (c == '\r') continue;
        if (n + 1 >= capacity) {
            out[n] = 0;
            note_bad_line(out);
            fail("Mac line too long");
            return false;
        }
        out[n++] = (char)c;
    }
}

/* Bulk transfer prevents USB event overhead for every individual pixel. */
static bool recv_exact(uint8_t *buf, size_t size, int seconds) {
    clock_t deadline = clock() + (clock_t)seconds * CLOCKS_PER_SEC;
    size_t got = 0;
    /* Bytes already buffered behind the header come first. */
    if (rx_pos < rx_len) {
        size_t take = (size_t)(rx_len - rx_pos);
        if (take > size) take = size;
        memcpy(buf, rx_buf + rx_pos, take);
        rx_pos = (uint8_t)(rx_pos + take);
        got = take;
    }
    while (got < size && clock() < deadline) {
        int n;
        usb_poll();
        scan_controls();
        if (quit_requested) return false;
        if (link_lost) { fail("USB link lost in tile data"); return false; }
        n = srl_Read(&srl, buf + got, size - got);
        if (n < 0) { fail_code("srl_Read error", n); return false; }
        if (n > 0) got += (size_t)n;
    }
    if (got != size && !quit_requested) fail("Timed out in tile data");
    return got == size;
}

static void palette_init(void) {
    unsigned i;
    for (i = 0; i < 256; ++i) {
        uint8_t r = (uint8_t)((((i >> 5) & 7) * 255) / 7);
        uint8_t g = (uint8_t)((((i >> 2) & 7) * 255) / 7);
        uint8_t b = (uint8_t)(((i & 3) * 255) / 3);
        gfx_palette[i] = gfx_RGBTo1555(r, g, b);
    }
}

static void cursor_hide(void) {
    int dx, dy, n = 0;
    if (!cursor_visible) return;
    for (dy = -4; dy <= 4; ++dy) {
        for (dx = -4; dx <= 4; ++dx) {
            int x = cursor_x + dx, y = cursor_y + dy;
            if (x >= 0 && x < SCREEN_W && y >= 0 && y < SCREEN_H)
                gfx_vbuffer[y][x] = cursor_bg[n];
            n++;
        }
    }
    cursor_visible = false;
}

static void cursor_show(void) {
    int dx, dy, n = 0;
    if (cursor_visible || typing) return;
    for (dy = -4; dy <= 4; ++dy) {
        for (dx = -4; dx <= 4; ++dx) {
            int x = cursor_x + dx, y = cursor_y + dy;
            cursor_bg[n++] = (x >= 0 && x < SCREEN_W && y >= 0 && y < SCREEN_H)
                ? gfx_vbuffer[y][x] : 0;
            if (x >= 0 && x < SCREEN_W && y >= 0 && y < SCREEN_H &&
                (dx == 0 || dy == 0)) {
                gfx_vbuffer[y][x] = (dx == 0 && dy == 0) ? 0 : 255;
            }
        }
    }
    cursor_visible = true;
}

static void cursor_move(int dx, int dy) {
    cursor_hide();
    cursor_x += dx;
    cursor_y += dy;
    if (cursor_x < 0) cursor_x = 0;
    if (cursor_x > 319) cursor_x = 319;
    if (cursor_y < 0) cursor_y = 0;
    if (cursor_y > 239) cursor_y = 239;
    cursor_show();
}

/* Report the local cursor position to the Mac (calculator pixel coordinates). */
static void send_cursor_position(void) {
    char command[28];
    snprintf(command, sizeof(command), "M|%d|%d\n", cursor_x, cursor_y);
    send_line(command);
}

/* TRACE sends a left click, GRAPH a right click at the current cursor. */
static void send_click(char which) {
    char command[32];
    snprintf(command, sizeof(command), "C|%c|%d|%d\n", which, cursor_x, cursor_y);
    send_line(command);
}

/* Header T|x|y|kind|len. P = raw 256 bytes, R = RGB332 RLE pairs,
   Q = packed: [n][n palette bytes][256 indices at 1/2/4 bits, MSB first]
   (Q only after the Mac answered @M84READY|5). */
static bool tile_header(const char *line, int *tx, int *ty, char *kind, int *len) {
    char *end;
    long x, y, n;
    if (line[0] != 'T' || line[1] != '|') return false;
    x = strtol(line + 2, &end, 10);
    if (*end++ != '|') return false;
    y = strtol(end, &end, 10);
    if (*end++ != '|' || !(*end == 'R' || *end == 'P' || *end == 'Q')) return false;
    *kind = *end++;
    if (*end++ != '|') return false;
    n = strtol(end, &end, 10);
    if (*end != 0 || x < 0 || x >= 20 || y < 0 || y >= 15 || n < 2 ||
        n > MAX_TILE_PACKET || (*kind == 'R' && (n & 1)) ||
        (*kind == 'P' && n != 256) ||
        (*kind == 'Q' && (n < 1 + 1 + 32 || n > 1 + 16 + 128))) return false;
    *tx = (int)x; *ty = (int)y; *len = (int)n;
    return true;
}

static uint8_t tile_px[TILE * TILE];

/* Decode fully (and validate) before touching the screen, so a bad packet
   never leaves a half-drawn tile. */
static const uint8_t *decode_tile(char kind, int packet_len) {
    int i, pixel = 0;
    if (kind == 'P') return packet;
    if (kind == 'R') {
        for (i = 0; i < packet_len; i += 2) {
            int run = packet[i];
            if (!run || pixel + run > TILE * TILE) return NULL;
            memset(tile_px + pixel, packet[i + 1], (size_t)run);
            pixel += run;
        }
        return pixel == TILE * TILE ? tile_px : NULL;
    }
    {
        int colors = packet[0], bits, shift;
        const uint8_t *palette = packet + 1, *src;
        uint8_t mask;
        if (colors < 1 || colors > 16) return NULL;
        bits = colors <= 2 ? 1 : colors <= 4 ? 2 : 4;
        if (packet_len != 1 + colors + 32 * bits) return NULL;
        mask = (uint8_t)((1 << bits) - 1);
        src = packet + 1 + colors;
        while (pixel < TILE * TILE) {
            uint8_t byte = *src++;
            for (shift = 8 - bits; shift >= 0; shift -= bits) {
                uint8_t index = (uint8_t)((byte >> shift) & mask);
                if (index >= colors) return NULL;
                tile_px[pixel++] = palette[index];
            }
        }
        return tile_px;
    }
}

static bool draw_tile(int tx, int ty, char kind, int packet_len) {
    const uint8_t *src = decode_tile(kind, packet_len);
    int row, x = tx * TILE, y = ty * TILE;
    if (!src) return false;
    /* One 16-byte copy per row instead of 256 indexed pixel writes. */
    for (row = 0; row < TILE; ++row, ++y) {
        if (typing && y >= OVERLAY_Y) break;
        memcpy(&gfx_vbuffer[y][x], src + row * TILE, TILE);
    }
    return true;
}

static bool fetch_frame(void) {
    char line[50];
    int total, i;
    char *sep;
    /* A new request starts a fresh diagnosis. */
    fail_reason[0] = 0;
    bad_line[0] = 0;
    ++batch_number;
    stage_tile = stage_total = 0;
    /* Safe point to report: the Mac is reading requests, not waiting for ACKs. */
    if (usb_event_errors != usb_errors_reported) {
        char note[40];
        snprintf(note, sizeof(note), "D|usb_event_error|%u|%d\n",
                 usb_event_errors, last_usb_error);
        usb_errors_reported = usb_event_errors;
        stage = "sending diagnostic";
        if (!send_line(note)) return false;
    }
    stage = "requesting update";
    if (!send_line("V\n")) return false;
    stage = "waiting for batch header";
    for (i = 0; i < 40; ++i) {
        if (!recv_line(line, sizeof(line), 8)) return false;
        if (!strncmp(line, "@M84READY", 9)) continue;  /* any version's reply */
        if (line[0] == 'F' && line[1] == '|') break;
        note_bad_line(line);
        fail("Unexpected line, wanted F|");
        return false;
    }
    if (i == 40) { fail("Too many READY lines"); return false; }
    total = atoi(line + 2);
    sep = strrchr(line, '|');
    if (!sep || sep == line + 1 || total < 0 || total > 36 ||
        (strcmp(sep + 1, "0") && strcmp(sep + 1, "1"))) {
        note_bad_line(line);
        fail("Bad batch header");
        return false;
    }
    next_batch = sep[1] == '1';
    stage_total = total;
    cursor_hide();
    for (i = 0; i < total; ++i) {
        int tx, ty, len;
        char kind;
        stage_tile = i + 1;
        stage = "waiting for tile header";
        if (!recv_line(line, sizeof(line), 8)) return false;
        if (!tile_header(line, &tx, &ty, &kind, &len)) {
            note_bad_line(line);
            fail("Bad tile header");
            return false;
        }
        stage = "receiving tile data";
        if (!recv_exact(packet, (size_t)len, 8)) return false;
        if (!draw_tile(tx, ty, kind, len)) {
            fail(kind == 'Q' ? "Bad packed tile data" : "Bad RLE tile data");
            return false;
        }
        /* The host sends groups of 4 tiles before waiting for an ACK. */
        if ((i + 1) % ACK_GROUP == 0 || i + 1 == total) {
            stage = "sending ACK";
            if (!send_line("A\n")) return false;
        }
    }
    stage = "waiting for batch end";
    if (!recv_line(line, sizeof(line), 8)) return false;
    if (strcmp(line, "E")) {
        note_bad_line(line);
        fail("Unexpected line, wanted E");
        return false;
    }
    stage = "idle";
    if (typing) input_redraw();
    else cursor_show();
    return true;
}

static bool hotkey(int group, uint8_t bit) {
    return (event_keys[group] & bit) != 0;
}
static bool shift_held(void) { return event_shift; }

static void queue_key(uint8_t g, uint8_t bit, bool shifted) {
    uint8_t next = (queue_tail + 1) % KEY_QUEUE;
    if (next == queue_head) return;  /* bounded, drop repeats if overwhelmed */
    key_queue[queue_tail].group = g;
    key_queue[queue_tail].bit = bit;
    key_queue[queue_tail].shifted = shifted;
    queue_tail = next;
}

/* Sample keys *during* serial transfers so brief presses are not lost. */
static void scan_controls(void) {
    uint8_t group, bit, fresh;
    bool shifted;
    clock_t now = clock();
    if (now - last_scan < CLOCKS_PER_SEC / 60) return;
    last_scan = now;
    kb_Scan();
    if ((kb_Data[1] & kb_2nd) && !(prev_keys[1] & kb_2nd))
        second_latched = !second_latched;
    shifted = (kb_Data[1] & kb_2nd) != 0 || second_latched;
    for (group = 1; group < 8; ++group) {
        fresh = kb_Data[group] & ~prev_keys[group];
        if (group == 1) fresh &= ~kb_2nd;
        for (bit = 1; bit; bit <<= 1) {
            if (fresh & bit) {
                if (group == 1 && bit == kb_Mode && (!typing || shifted))
                    quit_requested = true;
                else
                    queue_key(group, bit, shifted);
                second_latched = false;
            }
        }
        prev_keys[group] = kb_Data[group];
    }
    /* Holding the arrows repeats smoothly even while tiles are streaming. */
    if (now >= next_repeat) {
        for (bit = 1; bit <= kb_Up; bit <<= 1) {
            if ((kb_Data[7] & bit) && (prev_keys[7] & bit) &&
                !(fresh & bit)) queue_key(7, bit, shifted);
        }
        next_repeat = now + (clock_t)(CLOCKS_PER_SEC / 9);
    }
}

static void process_controls(void) {
    while (queue_head != queue_tail && !quit_requested) {
        struct KeyEvent e = key_queue[queue_head];
        queue_head = (queue_head + 1) % KEY_QUEUE;
        memset(event_keys, 0, sizeof(event_keys));
        event_keys[e.group] = e.bit;
        event_shift = e.shifted;
        if (typing) input_keys();
        else remote_keys();
    }
}

/* Bounded drawing: never wait for an LCD swap or USB packet in the editor. */
static void editor_string(const char *text, int x, int y) {
    int n, row, col;
    for (n = 0; text[n] && n < 39 && x + 8 <= SCREEN_W; ++n, x += 8) {
        unsigned char c = (unsigned char)text[n];
        if (c < 32 || c > 127) c = '?';
        for (row = 0; row < 8; ++row)
            for (col = 0; col < 8; ++col)
                gfx_vbuffer[y + row][x + col] =
                    (editor_font[c - 32][row] & (128 >> col)) ? 255 : 0;
    }
}

static void input_redraw(void) {
    int start = 0, count, caret, y;
    char visible[37];
    for (y = OVERLAY_Y; y < SCREEN_H; ++y)
        memset(gfx_vbuffer[y], 0, SCREEN_W);
    editor_string("ENTER:SEND MODE:CANCEL 2ND+MODE:QUIT", 3, OVERLAY_Y + 2);
    editor_string(edit_mode == 0 ? "LETTERS  2ND+ALPHA:CHANGE  2ND+0:SPACE" :
                  edit_mode == 1 ? "NUMBERS  2ND+ALPHA:CHANGE" :
                                   "SYMBOLS  2ND+ALPHA:CHANGE", 3, OVERLAY_Y + 14);
    if (edit_pos > 35) start = edit_pos - 35;
    count = edit_len - start;
    if (count > 36) count = 36;
    if (count) memcpy(visible, edit_text + start, (size_t)count);
    visible[count] = 0;
    editor_string(visible, 3, OVERLAY_Y + 27);
    caret = 3 + (edit_pos - start) * 8;
    for (y = OVERLAY_Y + 26; y < OVERLAY_Y + 36; ++y)
        gfx_vbuffer[y][caret] = 255;
}

static void input_start(void) {
    cursor_hide();
    typing = true;
    edit_len = edit_pos = edit_mode = 0;
    edit_text[0] = 0;
    input_redraw();
}

static void input_stop(bool send) {
    typing = false;
    if (send && edit_len) {
        char cmd[104];
        cmd[0] = 'K'; cmd[1] = '|';
        memcpy(cmd + 2, edit_text, edit_len);
        cmd[edit_len + 2] = '\n';
        cmd[edit_len + 3] = 0;
        send_line(cmd);
    }
    /* Resend the covered strip; no 15KB screen backup to copy or maintain. */
    send_line("I\n");
    force_refresh = true;
    cursor_show();
}

static void input_insert(char c) {
    if (!c || edit_len >= 100) return;
    memmove(edit_text + edit_pos + 1, edit_text + edit_pos,
            (size_t)(edit_len - edit_pos + 1));
    edit_text[edit_pos++] = c;
    ++edit_len;
    input_redraw();
}

static char letter_key(void) {
    /* TI-84 CE physical alpha legend (A through Z). */
    if (hotkey(2, kb_Math)) return 'a';
    if (hotkey(3, kb_Apps)) return 'b';
    if (hotkey(4, kb_Prgm)) return 'c';
    if (hotkey(2, kb_Recip)) return 'd';
    if (hotkey(3, kb_Sin)) return 'e';
    if (hotkey(4, kb_Cos)) return 'f';
    if (hotkey(5, kb_Tan)) return 'g';
    if (hotkey(6, kb_Power)) return 'h';
    if (hotkey(2, kb_Square)) return 'i';
    if (hotkey(3, kb_Comma)) return 'j';
    if (hotkey(4, kb_LParen)) return 'k';
    if (hotkey(5, kb_RParen)) return 'l';
    if (hotkey(6, kb_Div)) return 'm';
    if (hotkey(2, kb_Log)) return 'n';
    if (hotkey(2, kb_Ln)) return 'o';
    if (hotkey(2, kb_Sto)) return 'p';
    if (hotkey(3, kb_7)) return 'q';
    if (hotkey(4, kb_8)) return 'r';
    if (hotkey(5, kb_9)) return 's';
    if (hotkey(3, kb_4)) return 't';
    if (hotkey(4, kb_5)) return 'u';
    if (hotkey(5, kb_6)) return 'v';
    if (hotkey(3, kb_1)) return 'w';
    if (hotkey(4, kb_2)) return 'x';
    if (hotkey(5, kb_3)) return 'y';
    if (hotkey(3, kb_0)) return 'z';
    return 0;
}

static char number_key(void) {
    if (hotkey(3, kb_0)) return '0';
    if (hotkey(3, kb_1)) return '1';
    if (hotkey(4, kb_2)) return '2';
    if (hotkey(5, kb_3)) return '3';
    if (hotkey(3, kb_4)) return '4';
    if (hotkey(4, kb_5)) return '5';
    if (hotkey(5, kb_6)) return '6';
    if (hotkey(3, kb_7)) return '7';
    if (hotkey(4, kb_8)) return '8';
    if (hotkey(5, kb_9)) return '9';
    if (hotkey(4, kb_DecPnt)) return '.';
    return 0;
}

static char symbol_key(void) {
    if (hotkey(3, kb_0)) return ' ';
    if (hotkey(6, kb_Add)) return '+';
    if (hotkey(6, kb_Sub)) return '-';
    if (hotkey(6, kb_Mul)) return '*';
    if (hotkey(6, kb_Div)) return '/';
    if (hotkey(6, kb_Power)) return '^';
    if (hotkey(4, kb_DecPnt)) return '.';
    if (hotkey(3, kb_Comma)) return ',';
    if (hotkey(4, kb_LParen)) return '(';
    if (hotkey(5, kb_RParen)) return ')';
    if (hotkey(3, kb_1)) return '!';
    if (hotkey(4, kb_2)) return '?';
    if (hotkey(5, kb_3)) return ':';
    if (hotkey(3, kb_4)) return '@';
    if (hotkey(4, kb_5)) return '#';
    if (hotkey(5, kb_6)) return '=';
    if (hotkey(3, kb_7)) return '\'';
    if (hotkey(4, kb_8)) return '"';
    return 0;
}

/* Keep the reason on screen until the user acknowledges it. Never just exit. */
static bool any_key_down(void) {
    int g;
    for (g = 1; g < 8; ++g) if (kb_Data[g]) return true;
    return false;
}

static void wait_for_acknowledge(void) {
    clock_t until = clock() + CLOCKS_PER_SEC / 2;
    /* Ignore keys still held from before the stop, then wait for a new press. */
    while (true) {
        usb_poll();
        kb_Scan();
        if (!any_key_down() && clock() >= until) break;
    }
    while (true) {
        usb_poll();
        kb_Scan();
        if ((kb_Data[6] & (kb_Clear | kb_Enter)) || (kb_Data[1] & kb_Mode)) break;
    }
}

static void show_stop_screen(void) {
    char text[44];
    int y = 52;
    cursor_hide();
    gfx_SetColor(255);
    gfx_FillRectangle_NoClip(0, 48, 320, 120);
    gfx_SetColor(224);  /* RGB332 red border */
    gfx_FillRectangle_NoClip(0, 48, 320, 2);
    gfx_FillRectangle_NoClip(0, 166, 320, 2);
    gfx_SetTextBGColor(255);
    gfx_SetTextFGColor(224);
    gfx_PrintStringXY("MAC84 v0.3.4 STOPPED", 4, y); y += 14;
    gfx_SetTextFGColor(0);
    gfx_PrintStringXY(fail_reason[0] ? fail_reason : "No reason recorded", 4, y); y += 12;
    snprintf(text, sizeof(text), "While: %s", stage);
    gfx_PrintStringXY(text, 4, y); y += 12;
    snprintf(text, sizeof(text), "Batch %u, tile %d of %d", batch_number,
             stage_tile, stage_total);
    gfx_PrintStringXY(text, 4, y); y += 12;
    if (bad_line[0]) {
        snprintf(text, sizeof(text), "Got: %s", bad_line);
        gfx_PrintStringXY(text, 4, y);
    }
    y += 12;
    snprintf(text, sizeof(text), "USB event codes: %u (last %d)",
             usb_event_errors, last_usb_error);
    gfx_PrintStringXY(text, 4, y); y += 12;
    snprintf(text, sizeof(text), "Link lost: %s", link_lost ? "yes" : "no");
    gfx_PrintStringXY(text, 4, y); y += 16;
    gfx_PrintStringXY("Note this, then press CLEAR to exit", 4, y);
    wait_for_acknowledge();
}

static void input_keys(void) {
    char c;
    if (hotkey(6, kb_Clear)) {
        edit_len = edit_pos = 0; edit_text[0] = 0; input_redraw(); return;
    }
    if (hotkey(1, kb_Mode)) { input_stop(false); return; }
    if (hotkey(6, kb_Enter)) { input_stop(true); return; }
    if (hotkey(2, kb_Alpha)) {
        edit_mode = shift_held() ? (uint8_t)((edit_mode + 1) % 3) : 0;
        input_redraw();
        return;
    }
    if (hotkey(1, kb_Del) && edit_pos > 0) {
        memmove(edit_text + edit_pos - 1, edit_text + edit_pos,
                (size_t)(edit_len - edit_pos + 1));
        --edit_pos; --edit_len;
        input_redraw();
        return;
    }
    if (hotkey(7, kb_Left) && edit_pos > 0) { --edit_pos; input_redraw(); return; }
    if (hotkey(7, kb_Right) && edit_pos < edit_len) { ++edit_pos; input_redraw(); return; }
    if (shift_held() && hotkey(3, kb_0)) { input_insert(' '); return; }
    c = edit_mode == 0 ? letter_key() : edit_mode == 1 ? number_key() : symbol_key();
    input_insert(c);
}

static void remote_keys(void) {
    int dx = 0, dy = 0;
    bool pan = shift_held();
    int step = zoom_level == 0 ? 3 : zoom_level == 1 ? 4 : zoom_level == 2 ? 6 : 8;
    if (hotkey(7, kb_Left)) dx = pan ? -60 : -step;
    if (hotkey(7, kb_Right)) dx = pan ? 60 : step;
    if (hotkey(7, kb_Up)) dy = pan ? -48 : -step;
    if (hotkey(7, kb_Down)) dy = pan ? 48 : step;
    if (dx || dy) {
        if (pan) {
            char cmd[32];
            snprintf(cmd, sizeof(cmd), "P|%d|%d\n", dx, dy);
            send_line(cmd);
            force_refresh = true;
        } else {
            cursor_move(dx, dy);
            send_cursor_position();
        }
    }
    /* Top row, left to right: Y= look, WINDOW windows, ZOOM double, TRACE
       left, GRAPH right. */
    if (hotkey(1, kb_Trace)) { send_click('L'); force_refresh = true; }
    if (hotkey(1, kb_Graph)) { send_click('R'); force_refresh = true; }
    if (hotkey(1, kb_Zoom)) { send_click('D'); force_refresh = true; }
    if (hotkey(1, kb_Yequ)) {
        char cmd[24];
        snprintf(cmd, sizeof(cmd), "L|%d|%d\n", cursor_x, cursor_y);
        send_line(cmd);
        force_refresh = true;
    }
    if (hotkey(1, kb_Window)) { send_line(pan ? "X|A\n" : "X|M\n"); force_refresh = true; }
    if (hotkey(6, kb_Power)) { send_line("W|3\n"); force_refresh = true; }
    if (hotkey(6, kb_Div)) { send_line("W|-3\n"); force_refresh = true; }
    if (hotkey(6, kb_Add)) {
        if (zoom_level < 4) ++zoom_level;
        send_line("Z|+\n"); force_refresh = true;
    }
    if (hotkey(6, kb_Sub)) {
        if (zoom_level > 0) --zoom_level;
        send_line("Z|-\n"); force_refresh = true;
    }
    if (hotkey(5, kb_Vars)) { send_line("S|L\n"); force_refresh = true; }
    if (hotkey(6, kb_Clear)) { send_line("S|R\n"); force_refresh = true; }
    if (hotkey(2, kb_Alpha)) input_start();
}

int main(void) {
    usb_device_t host;
    clock_t last_refresh;
    char line[40];
    int i;
    gfx_Begin();
    gfx_SetDrawScreen();
    palette_init();
    gfx_FillScreen(255);
    gfx_SetTextFGColor(0);
    gfx_PrintStringXY("MAC84 v0.3.4", 4, 6);
    gfx_PrintStringXY("Waiting for USB...", 4, 22);
    if (usb_Init(usb_callback, NULL,
                 srl_GetCDCStandardDescriptors(), USB_DEFAULT_INIT_FLAGS) != USB_SUCCESS) {
        gfx_PrintStringXY("USB init failed", 4, 38);
        goto cleanup_graphics;
    }
    host = NULL;
    while (!host) {
        usb_poll();
        host = usb_FindDevice(NULL, NULL, USB_SKIP_HUBS);
        kb_Scan();
        if (kb_Data[1] & kb_Mode) goto cleanup_usb;
    }
    if (srl_Open(&srl, host, serial_buffer, sizeof(serial_buffer),
                 SRL_INTERFACE_ANY, 9600) != SRL_SUCCESS) {
        gfx_PrintStringXY("Serial open failed", 4, 38);
        goto cleanup_usb;
    }
    link_device = host;
    link_open = true;
    /* The bridge might not be open when the calculator enumerates.
       Retry HELLO until it appears, rather than sending once and giving up.
       This also accepts the bridge's proactive READY on port-open. */
    gfx_PrintStringXY("Waiting for Mac bridge...", 4, 38);
    stage = "handshake";
    for (i = 0; i < 40; ++i) {
        fail_reason[0] = 0;  /* a retry timeout is expected here, not a cause */
        if (!send_line("@M84|5\n")) {
            gfx_PrintStringXY("USB write failed", 4, 54);
            gfx_PrintStringXY(fail_reason, 4, 70);
            goto wait_for_exit;
        }
        if (recv_line(line, sizeof(line), 2) && !strncmp(line, "@M84READY", 9)) {
            online = true;
            break;
        }
        kb_Scan();
        if (kb_Data[1] & kb_Mode) goto cleanup_serial;
    }
    if (!online) {
        gfx_PrintStringXY("No bridge after 80 sec", 4, 54);
        goto wait_for_exit;
    }
    /* A plain @M84READY may be the bridge's proactive port-open READY, sent
       before it saw our greeting. Repeat the greeting so a v0.3.4 bridge
       enables packed tiles; an older bridge just logs and ignores it. */
    if (strcmp(line, "@M84READY|5") && !send_line("@M84|5\n")) {
        show_stop_screen();
        goto cleanup_serial;
    }
    gfx_FillScreen(255);
    cursor_show();
    last_scan = 0;
    next_repeat = clock() + CLOCKS_PER_SEC / 4;
    last_refresh = clock() - CLOCKS_PER_SEC;
    while (!quit_requested && !link_lost) {
        usb_poll();
        scan_controls();
        process_controls();
        if (quit_requested || link_lost) break;
        if (force_refresh || next_batch ||
            (clock() - last_refresh) > (clock_t)(CLOCKS_PER_SEC * 3 / 4)) {
            force_refresh = false;
            if (!fetch_frame()) {
                /* v0.3.2 drew an error here and then exited immediately,
                   erasing it. Stop the loop; the reason is shown below. */
                break;
            }
            process_controls();
            last_refresh = clock();
        }
    }
    if (quit_requested && !link_lost) {
        /* Intentional quit: brief confirmation so an unexpected quit is
           distinguishable from a crash. */
        cursor_hide();
        gfx_SetColor(255);
        gfx_FillRectangle_NoClip(0, 108, 320, 24);
        gfx_SetTextFGColor(0);
        gfx_SetTextBGColor(255);
        gfx_PrintStringXY("MAC84: quit by MODE key", 4, 116);
        {
            clock_t until = clock() + CLOCKS_PER_SEC * 3 / 4;
            while (clock() < until) usb_poll();
        }
    } else {
        show_stop_screen();
    }
    goto cleanup_serial;
wait_for_exit:
    /* Keep the diagnostic on screen until MODE, instead of immediately
       exiting and making macOS report Errno 6 (device disconnected). */
    while (!link_lost) {
        usb_poll();
        kb_Scan();
        if (kb_Data[1] & kb_Mode) break;
    }
cleanup_serial:
    if (!link_lost) srl_Close(&srl);
    link_open = false;
cleanup_usb:
    usb_Cleanup();
cleanup_graphics:
    gfx_End();
    return 0;
}
