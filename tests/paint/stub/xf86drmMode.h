/* The KMS calls the painter makes to find its outputs, tell which one
 * each window is on, and set their DPMS. */
#ifndef FAKE_XF86DRMMODE_H
#define FAKE_XF86DRMMODE_H

#include <stdint.h>

#define DRM_MODE_CONNECTED 1
#define DRM_MODE_DISCONNECTED 2
#define DRM_MODE_DPMS_ON 0
#define DRM_MODE_DPMS_OFF 3
#define DRM_MODE_CONNECTOR_HDMIA 11

typedef struct {
    int count_connectors;
    uint32_t *connectors;
} drmModeRes;

typedef struct {
    uint32_t connector_id, encoder_id;
    uint32_t connector_type, connector_type_id;
    int connection;
    int count_props;
    uint32_t *props;
} drmModeConnector;

typedef struct {
    uint32_t encoder_id, crtc_id;
} drmModeEncoder;

typedef struct {
    uint32_t crtc_id, buffer_id;
} drmModeCrtc;

#define DRM_MODE_PROP_ENUM (1 << 3)

struct drm_mode_property_enum {
    uint64_t value;
    char name[32];
};

typedef struct {
    uint32_t prop_id;
    uint32_t flags;
    char name[32];
    int count_enums;
    struct drm_mode_property_enum *enums;
} drmModePropertyRes;

drmModeRes *drmModeGetResources(int fd);
void drmModeFreeResources(drmModeRes *r);
drmModeConnector *drmModeGetConnector(int fd, uint32_t id);
void drmModeFreeConnector(drmModeConnector *c);
drmModeEncoder *drmModeGetEncoder(int fd, uint32_t id);
void drmModeFreeEncoder(drmModeEncoder *e);
drmModeCrtc *drmModeGetCrtc(int fd, uint32_t id);
void drmModeFreeCrtc(drmModeCrtc *c);
drmModePropertyRes *drmModeGetProperty(int fd, uint32_t id);
void drmModeFreeProperty(drmModePropertyRes *p);
int drmModeConnectorSetProperty(int fd, uint32_t connector, uint32_t prop, uint64_t value);

#endif
