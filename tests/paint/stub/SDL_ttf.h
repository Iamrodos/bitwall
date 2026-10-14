/* SDL_ttf, as a font whose cells are size/2+1 wide and size+2 tall. */
#ifndef FAKE_SDL_TTF_H
#define FAKE_SDL_TTF_H

#include "SDL.h"

typedef struct TTF_Font TTF_Font;

int TTF_Init(void);
void TTF_Quit(void);
TTF_Font *TTF_OpenFont(const char *path, int size);
void TTF_CloseFont(TTF_Font *f);
int TTF_SizeUTF8(TTF_Font *f, const char *text, int *w, int *h);
int TTF_FontLineSkip(const TTF_Font *f);
SDL_Surface *TTF_RenderUTF8_Blended(TTF_Font *f, const char *text, SDL_Color fg);
SDL_Surface *TTF_RenderUTF8_Shaded(TTF_Font *f, const char *text, SDL_Color fg, SDL_Color bg);
int TTF_GlyphIsProvided32(TTF_Font *f, Uint32 cp);
#define TTF_GetError SDL_GetError

#endif
