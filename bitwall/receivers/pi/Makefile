# The painter, built for the machine it runs on.
#
#     make                    a Pi: KMS, GLES 2, V4L2
#     make PLATFORM=pi
#     make PLATFORM=mac       a Mac: a window or no screen, desktop GL 2.1,
#                             VideoToolbox (brew install sdl2 sdl2_ttf
#                             libpng jpeg-turbo ffmpeg pkg-config)
#     make PLATFORM=windows   Windows, in MSYS2's UCRT64 shell: a window or
#                             no screen, GL 2.1 through WGL, D3D11VA or QSV,
#                             named pipes (pacman: mingw-w64-ucrt-x86_64-
#                             gcc, -SDL2, -SDL2_ttf, -libpng, -libjpeg-turbo,
#                             -pkgconf; make)
#
# It is one cc of bitwall-paint.c, which includes the rest: platform.h for
# the seams between the compositor and the machine, and each seam's
# implementation for the platform. A platform is the settings below, and
# its own files where a seam differs.
#
# The version is the first twelve hex digits of the SHA-256 of SOURCES one
# after the other, in this order, which is bitwall/receivers/__init__.py's
# order too, so a controller works out the same version from its copy.

PLATFORM ?= pi
CC ?= cc
# Where the painter is written; the tests build theirs elsewhere.
OUT ?= bitwall-paint

SOURCES = bitwall-paint.c qoi.h platform.h display-kms.c gpu-gles.c child-posix.c \
          probe-v4l2.c machine-linux.c display-sdl.c gpu-gl.c probe-mac.c machine-mac.c \
          outputs.c ipc-posix.c ipc-win.c compat-win.h gpu-win.c child-win.c probe-win.c \
          machine-win.c pool.c

ifeq ($(PLATFORM),pi)
CFLAGS = -std=c99 -O2 -Wall -Wextra
DEFINES = -DBITWALL_JPEG
PACKAGES = sdl2 SDL2_ttf libdrm glesv2 libpng libjpeg
LIBS = -lpthread
else ifeq ($(PLATFORM),mac)
CFLAGS = -std=c99 -O2 -Wall -Wextra
DEFINES = -DBITWALL_MAC -DBITWALL_JPEG -D_DARWIN_C_SOURCE -DSDL_USE_BUILTIN_OPENGL_DEFINITIONS
PACKAGES = sdl2 SDL2_ttf libpng libjpeg
LIBS = -lpthread -framework OpenGL
else ifeq ($(PLATFORM),windows)
CC = gcc
CFLAGS = -std=c99 -O2 -Wall -Wextra
DEFINES = -DBITWALL_WIN -DBITWALL_JPEG -DSDL_USE_BUILTIN_OPENGL_DEFINITIONS -DGL_APICALL= \
          -DSDL_MAIN_HANDLED
PACKAGES = sdl2 SDL2_ttf libpng libjpeg
LIBS = -lopengl32 -lws2_32 -liphlpapi -mconsole
else
$(error no settings for PLATFORM=$(PLATFORM))
endif

VERSION := $(shell cat $(SOURCES) | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-12)

$(OUT): $(SOURCES)
	$(CC) $(CFLAGS) -DBITWALL_VERSION="\"$(VERSION)\"" $(DEFINES) \
	   -o $@ bitwall-paint.c \
	   $$(pkg-config --cflags --libs $(PACKAGES)) $(LIBS)

.PHONY: version
version:
	@echo $(VERSION)
