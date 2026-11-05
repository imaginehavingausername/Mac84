#include <stdint.h>
extern uint8_t gfx_vbuffer[240][320];
extern uint16_t gfx_palette[256];
#define gfx_RGBTo1555(r,g,b) 0
void gfx_Begin(void);
void gfx_End(void);
void gfx_SetDrawScreen(void);
void gfx_FillScreen(int);
void gfx_SetTextFGColor(int);
void gfx_SetTextBGColor(int);
void gfx_SetColor(int);
void gfx_PrintStringXY(const char *,int,int);
void gfx_FillRectangle_NoClip(int,int,int,int);
uint8_t *gfx_SetCharData(uint8_t,const uint8_t *);
