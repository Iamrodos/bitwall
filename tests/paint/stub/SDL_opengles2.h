/*
 * The GLES2 calls the painter makes. The fake in fake.c keeps the atlas
 * texture in memory, so a test can read back which mask a cell shows.
 */
#ifndef FAKE_SDL_OPENGLES2_H
#define FAKE_SDL_OPENGLES2_H

#include <stddef.h>

typedef unsigned int GLuint, GLenum, GLbitfield;
typedef int GLint, GLsizei;
typedef float GLfloat, GLclampf;
typedef unsigned short GLushort;
typedef unsigned char GLboolean, GLubyte;
typedef char GLchar;
typedef ptrdiff_t GLsizeiptr;

#define GL_FALSE 0
#define GL_TRUE 1
#define GL_NO_ERROR 0
#define GL_POINTS 0x0000
#define GL_TRIANGLES 0x0004
#define GL_COLOR_BUFFER_BIT 0x4000
#define GL_BLEND 0x0BE2
#define GL_CULL_FACE 0x0B44
#define GL_DEPTH_TEST 0x0B71
#define GL_UNPACK_ALIGNMENT 0x0CF5
#define GL_MAX_TEXTURE_SIZE 0x0D33
#define GL_ALIASED_POINT_SIZE_RANGE 0x846D
#define GL_TEXTURE_2D 0x0DE1
#define GL_TEXTURE0 0x84C0
#define GL_TEXTURE_MAG_FILTER 0x2800
#define GL_TEXTURE_MIN_FILTER 0x2801
#define GL_TEXTURE_WRAP_S 0x2802
#define GL_TEXTURE_WRAP_T 0x2803
#define GL_NEAREST 0x2600
#define GL_LINEAR 0x2601
#define GL_FRAMEBUFFER 0x8D40
#define GL_COLOR_ATTACHMENT0 0x8CE0
#define GL_FRAMEBUFFER_COMPLETE 0x8CD5
#define GL_CLAMP_TO_EDGE 0x812F
#define GL_UNSIGNED_BYTE 0x1401
#define GL_UNSIGNED_SHORT 0x1403
#define GL_FLOAT 0x1406
#define GL_SRC_ALPHA 0x0302
#define GL_ONE 1
#define GL_ONE_MINUS_SRC_ALPHA 0x0303
#define GL_ALPHA 0x1906
#define GL_RGBA 0x1908
#define GL_LUMINANCE 0x1909
#define GL_ARRAY_BUFFER 0x8892
#define GL_ELEMENT_ARRAY_BUFFER 0x8893
#define GL_STREAM_DRAW 0x88E0
#define GL_STATIC_DRAW 0x88E4
#define GL_FRAGMENT_SHADER 0x8B30
#define GL_VERTEX_SHADER 0x8B31
#define GL_COMPILE_STATUS 0x8B81
#define GL_LINK_STATUS 0x8B82
#define GL_VERSION 0x1F02
#define GL_PIXEL_PACK_BUFFER 0x88EB

void glActiveTexture(GLenum t);
void glAttachShader(GLuint p, GLuint s);
void glBindBuffer(GLenum target, GLuint b);
void glBindTexture(GLenum target, GLuint t);
void glBlendFunc(GLenum sfactor, GLenum dfactor);
void glBufferData(GLenum target, GLsizeiptr n, const void *data, GLenum usage);
void glClear(GLbitfield mask);
void glClearColor(GLfloat r, GLfloat g, GLfloat b, GLfloat a);
void glCompileShader(GLuint s);
GLuint glCreateProgram(void);
GLuint glCreateShader(GLenum kind);
void glDeleteProgram(GLuint p);
void glDeleteShader(GLuint s);
void glDisable(GLenum cap);
void glEnable(GLenum cap);
void glDrawArrays(GLenum mode, GLint first, GLsizei n);
void glDrawElements(GLenum mode, GLsizei n, GLenum type, const void *index);
void glEnableVertexAttribArray(GLuint a);
void glDisableVertexAttribArray(GLuint a);
void glDeleteBuffers(GLsizei n, const GLuint *b);
void glGenBuffers(GLsizei n, GLuint *out);
void glGenTextures(GLsizei n, GLuint *out);
void glDeleteTextures(GLsizei n, const GLuint *names);
void glGenFramebuffers(GLsizei n, GLuint *out);
void glDeleteFramebuffers(GLsizei n, const GLuint *f);
void glBindFramebuffer(GLenum t, GLuint f);
void glFramebufferTexture2D(GLenum t, GLenum a, GLenum tt, GLuint x, GLint l);
GLenum glCheckFramebufferStatus(GLenum t);
GLint glGetAttribLocation(GLuint p, const GLchar *name);
GLenum glGetError(void);
const GLubyte *glGetString(GLenum name);
void glGetFloatv(GLenum what, GLfloat *out);
void glGetIntegerv(GLenum what, GLint *out);
void glGetProgramInfoLog(GLuint p, GLsizei max, GLsizei *n, GLchar *log);
void glGetProgramiv(GLuint p, GLenum what, GLint *out);
void glGetShaderInfoLog(GLuint s, GLsizei max, GLsizei *n, GLchar *log);
void glGetShaderiv(GLuint s, GLenum what, GLint *out);
GLint glGetUniformLocation(GLuint p, const GLchar *name);
void glLinkProgram(GLuint p);
void glPixelStorei(GLenum what, GLint v);
void glReadPixels(GLint x, GLint y, GLsizei w, GLsizei h, GLenum f, GLenum t, void *out);
void glShaderSource(GLuint s, GLsizei n, const GLchar *const *text, const GLint *len);
void glTexImage2D(GLenum target, GLint level, GLint ifmt, GLsizei w, GLsizei h,
                  GLint border, GLenum fmt, GLenum type, const void *data);
void glTexParameteri(GLenum target, GLenum what, GLint v);
void glTexSubImage2D(GLenum target, GLint level, GLint x, GLint y, GLsizei w,
                     GLsizei h, GLenum fmt, GLenum type, const void *data);
void glUniform1f(GLint at, GLfloat v);
void glUniform1i(GLint at, GLint v);
void glUniform2f(GLint at, GLfloat a, GLfloat b);
void glUniform4f(GLint at, GLfloat a, GLfloat b, GLfloat c, GLfloat d);
void glUseProgram(GLuint p);
void glVertexAttribPointer(GLuint a, GLint n, GLenum type, GLboolean norm,
                           GLsizei stride, const void *at);
void glViewport(GLint x, GLint y, GLsizei w, GLsizei h);

#endif
