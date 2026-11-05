#include <stddef.h>
typedef int srl_device_t;
#define SRL_INTERFACE_ANY 0
#define SRL_SUCCESS 0
int srl_UsbEventCallback(int,void*,void*);
int srl_Write(void*,const void*,size_t);
int srl_Read(void*,void*,size_t);
void *srl_GetCDCStandardDescriptors(void);
int srl_Open(void*,void*,void*,size_t,int,int);
void srl_Close(void*);
