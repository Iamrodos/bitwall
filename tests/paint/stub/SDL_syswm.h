/* What SDL says about the window's video driver: only the KMS part. */
#ifndef FAKE_SDL_SYSWM_H
#define FAKE_SDL_SYSWM_H

#include "SDL.h"

typedef struct { Uint8 major, minor, patch; } SDL_version;
typedef enum { SDL_SYSWM_UNKNOWN, SDL_SYSWM_KMSDRM } SDL_SYSWM_TYPE;
typedef struct {
    SDL_version version;
    SDL_SYSWM_TYPE subsystem;
    union {
        struct { int dev_index; int drm_fd; void *gbm_dev; } kmsdrm;
    } info;
} SDL_SysWMinfo;

#define SDL_VERSION(v) ((v)->major = 2, (v)->minor = 0, (v)->patch = 0)

SDL_bool SDL_GetWindowWMInfo(SDL_Window *win, SDL_SysWMinfo *info);

#endif
