#include <stddef.h>
typedef int usb_error_t;
typedef int usb_event_t;
typedef void usb_callback_data_t;
typedef void *usb_device_t;
#define USB_SUCCESS 0
#define USB_DEVICE_DISCONNECTED_EVENT 1
#define USB_DEFAULT_INIT_FLAGS 0
#define USB_SKIP_HUBS 0
int usb_HandleEvents(void);
int usb_Init(int (*)(int,void*,void*),void*,void*,int);
void *usb_FindDevice(void*,void*,int);
void usb_Cleanup(void);
