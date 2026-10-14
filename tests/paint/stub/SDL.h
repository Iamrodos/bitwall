/*
 * The slice of SDL2 the painter uses, declared so that it compiles and
 * runs anywhere with a C compiler. The definitions are in fake.c: a
 * window that is only a pointer, surfaces in plain memory, and threads,
 * mutexes and conditions over pthreads, so the reader thread is real.
 */
#ifndef FAKE_SDL_H
#define FAKE_SDL_H

#include <stdint.h>

typedef uint8_t Uint8;
typedef uint16_t Uint16;
typedef uint32_t Uint32;
typedef enum { SDL_FALSE, SDL_TRUE } SDL_bool;

typedef struct { int x, y, w, h; } SDL_Rect;
typedef struct { Uint8 r, g, b, a; } SDL_Color;
typedef struct {
    Uint32 format;
    Uint8 BitsPerPixel, BytesPerPixel;
    Uint32 Amask;
    Uint8 Ashift;
} SDL_PixelFormat;
typedef struct {
    SDL_PixelFormat *format;
    int w, h, pitch;
    void *pixels;
} SDL_Surface;
typedef struct { Uint32 format; int w, h, refresh_rate; void *driverdata; } SDL_DisplayMode;
typedef enum { SDL_BLENDMODE_NONE, SDL_BLENDMODE_BLEND } SDL_BlendMode;

typedef struct SDL_Window SDL_Window;
typedef void *SDL_GLContext;
typedef struct SDL_mutex SDL_mutex;
typedef struct SDL_cond SDL_cond;
typedef struct SDL_Thread SDL_Thread;
typedef int (*SDL_ThreadFunction)(void *);

#define SDL_INIT_VIDEO 0x20u
#define SDL_DISABLE 0
#define SDL_WINDOW_FULLSCREEN 0x1u
#define SDL_WINDOW_OPENGL 0x2u
#define SDL_WINDOWPOS_UNDEFINED_DISPLAY(d) (0x1FFF0000u | (unsigned)(d))
#define SDL_PIXELFORMAT_ARGB8888 0x16362004u
#define SDL_PIXELFORMAT_ABGR8888 0x16762004u
#define SDL_HINT_NO_SIGNAL_HANDLERS "SDL_NO_SIGNAL_HANDLERS"

typedef enum {
    SDL_GL_DOUBLEBUFFER, SDL_GL_CONTEXT_MAJOR_VERSION,
    SDL_GL_CONTEXT_MINOR_VERSION, SDL_GL_CONTEXT_PROFILE_MASK
} SDL_GLattr;
#define SDL_GL_CONTEXT_PROFILE_ES 4

int SDL_Init(Uint32 flags);
void SDL_Quit(void);
const char *SDL_GetError(void);
SDL_bool SDL_SetHint(const char *name, const char *value);
int SDL_ShowCursor(int toggle);
void SDL_Delay(Uint32 ms);
void SDL_PumpEvents(void);

int SDL_GetNumVideoDisplays(void);
int SDL_GetCurrentDisplayMode(int display, SDL_DisplayMode *mode);
SDL_DisplayMode *SDL_GetClosestDisplayMode(int display, const SDL_DisplayMode *want,
                                           SDL_DisplayMode *got);
int SDL_SetWindowDisplayMode(SDL_Window *win, const SDL_DisplayMode *mode);
int SDL_SetWindowFullscreen(SDL_Window *win, Uint32 flags);
const char *SDL_GetCurrentVideoDriver(void);
SDL_Window *SDL_CreateWindow(const char *title, int x, int y, int w, int h, Uint32 flags);
void SDL_DestroyWindow(SDL_Window *win);
void SDL_GetWindowSize(SDL_Window *win, int *w, int *h);
SDL_Surface *SDL_GetWindowSurface(SDL_Window *win);
int SDL_UpdateWindowSurface(SDL_Window *win);
int SDL_UpdateWindowSurfaceRects(SDL_Window *win, const SDL_Rect *r, int n);

int SDL_GL_SetAttribute(SDL_GLattr attr, int value);
SDL_GLContext SDL_GL_CreateContext(SDL_Window *win);
void SDL_GL_DeleteContext(SDL_GLContext ctx);
int SDL_GL_SetSwapInterval(int interval);
void SDL_GL_SwapWindow(SDL_Window *win);
int SDL_GL_MakeCurrent(SDL_Window *win, SDL_GLContext ctx);
void *SDL_GL_GetProcAddress(const char *name);

SDL_Surface *SDL_CreateRGBSurfaceWithFormat(Uint32 flags, int w, int h, int depth, Uint32 format);
SDL_Surface *SDL_ConvertSurface(SDL_Surface *src, const SDL_PixelFormat *fmt, Uint32 flags);
void SDL_FreeSurface(SDL_Surface *s);
int SDL_LockSurface(SDL_Surface *s);
void SDL_UnlockSurface(SDL_Surface *s);
int SDL_FillRect(SDL_Surface *dst, const SDL_Rect *r, Uint32 colour);
int SDL_FillRects(SDL_Surface *dst, const SDL_Rect *r, int n, Uint32 colour);
Uint32 SDL_MapRGB(const SDL_PixelFormat *fmt, Uint8 r, Uint8 g, Uint8 b);
int SDL_BlitSurface(SDL_Surface *src, const SDL_Rect *sr, SDL_Surface *dst, SDL_Rect *dr);
int SDL_SetSurfaceBlendMode(SDL_Surface *s, SDL_BlendMode mode);
int SDL_SaveBMP(SDL_Surface *s, const char *path);

SDL_mutex *SDL_CreateMutex(void);
void SDL_DestroyMutex(SDL_mutex *m);
int SDL_LockMutex(SDL_mutex *m);
int SDL_UnlockMutex(SDL_mutex *m);
SDL_cond *SDL_CreateCond(void);
int SDL_CondSignal(SDL_cond *c);
int SDL_CondBroadcast(SDL_cond *c);
int SDL_CondWait(SDL_cond *c, SDL_mutex *m);
int SDL_CondWaitTimeout(SDL_cond *c, SDL_mutex *m, Uint32 ms);
SDL_Thread *SDL_CreateThread(SDL_ThreadFunction fn, const char *name, void *data);
void SDL_WaitThread(SDL_Thread *t, int *status);
void SDL_DetachThread(SDL_Thread *t);
int SDL_GetCPUCount(void);
int SDL_GetSystemRAM(void);

#endif
