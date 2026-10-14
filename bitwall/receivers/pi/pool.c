/*
 * The worker pool: the pictures a wall decodes and encodes, made on
 * threads of their own, so the main thread, which owns the GPU, only
 * draws, composes, uploads and reads back. A video layer's frames (QOI,
 * PNG, JPEG) are decoded here, and a picture or a mirror of the glass,
 * or of a stream's frame, is encoded here, QOI or JPEG, once the main
 * thread has read it back. Part of bitwall-paint.c, which includes it.
 *
 * A job carries its own copy of everything it reads and touches nothing
 * else: no session, no screen, no GL. Whoever made it takes it back on the
 * main thread, from `pool_take`, once a worker is done; the worker rings
 * the bell, so a loop asleep in `inbox_wait` wakes for a finished job as
 * it does for bytes. A job whose session has gone comes back with no
 * owner and is let go.
 *
 * Order is kept by those who ask, not by the pool. A video layer's frames
 * wait in a queue of its own and go up in the order sent, however long
 * each takes; it reads on past a frame decoding only to the next frame's
 * line and bytes, FRAMES_AHEAD of them, so two workers share one layer's
 * frames, and an answer to one of those lines waits its turn with them.
 * Any other command waits until the frames before it are up. A picture
 * holds its place in its session's `out` with an item that waits for its
 * bytes, so nothing said after it overtakes it.
 *
 * Its size is `--workers`, by default half the cores, from one to four.
 * With none, a job is done on the main thread as it is handed over and
 * taken back on the next turn of the loop, in the same order.
 */

enum { JOB_FRAME, JOB_QOI, JOB_SAY };

#define POOL_MAX 16
/* How many of one layer's frames may be with the pool at once. */
#define FRAMES_AHEAD 2

typedef struct Job {
    struct Job *next;           /* in the queue, then on the done list */
    int kind;
    struct Session *owner;      /* the main thread's alone; NULL once it has gone */
    struct Job *after;          /* the main thread's: the owner's next, in the
                                 * order it sent them */
    int back;                   /* the main thread's: back from the pool */
    char *say;                  /* JOB_SAY: a line, said in its turn */

    /* JOB_FRAME: a video layer's frame as its line and bytes said it */
    int codec;
    uint8_t *data;
    size_t len;
    int fw, fh, fx, fy, fsw, fsh, fhold;
    char fid[33];

    /* JOB_QOI: pixels read back, RGBA or YUV 4:2:0, to be QOI, or JPEG
     * at `quality` where that is above 0; summed where a mirror sends only
     * what changed */
    uint8_t *px;
    int w, h, yuv, sum_it, quality;
    struct OutItem *item;       /* its place in the owner's `out` */
    int mirror;                 /* the mirror it is for, or -1 for a picture */
    char head[200];             /* the line before its bytes, all but the count */
    char tag[40];               /* after the count: an id, or nothing */
    char name[33];              /* what it is a picture of */

    /* what the worker made */
    uint8_t *rgba;
    int pw, ph;
    void *q;
    int qlen;
    uint32_t sum;
    double ms;                  /* how long the worker took */
} Job;

static struct {
    SDL_mutex *mu;
    SDL_cond *work;
    Job *head, *tail;           /* waiting for a worker, under `mu` */
    Job *done, *done_tail;      /* finished, under the bell's lock */
    SDL_Thread *th[POOL_MAX];
    int size;                   /* workers running */
    int out;                    /* the main thread's: handed over, not yet taken back */
    int started, stop;
    void (*hook)(Job *);        /* the tests' way to hold a job up */
} pool;

/* The knobs, as `report` says them: the pool's size, and ffmpeg's threads
 * and nice. -1 until worked out from the machine or given. ffmpeg's
 * threads are two: `decoder_threads` for a stream a hardware decoder
 * takes, `decoder_soft` for one ffmpeg decodes on the CPU; --decoder-threads
 * gives both. */
static int pool_want = -1;
static int decoder_threads = -1;
static int decoder_soft = -1;
static int decoder_nice = -1;

static uint8_t *image_rgba(int codec, const uint8_t *data, size_t len, int *w, int *h);
static Uint32 yuv_argb(const uint8_t *px, int w, int h, int x, int y);
#ifdef BITWALL_JPEG
static void *jpeg_make(const uint8_t *rgba, int w, int h, int quality, int *len);
#endif

/* A sum of a look at the glass, to tell a look that changed from one that
 * did not. */
static uint32_t look_sum(const uint8_t *p, size_t n)
{
    uint32_t h = 2166136261u;
    size_t i;
    for (i = 0; i < n; i++)
        h = (h ^ p[i]) * 16777619u;
    return h;
}

/* The cores, and what the knobs are where nobody said: half the cores for
 * the pool, from one to four, since the main thread, the readers and
 * ffmpeg want the rest; one thread for ffmpeg where a hardware decoder
 * does the work, since each of ffmpeg's frame threads holds a frame on
 * its way even then, and one is fast enough; as many as the pool where
 * ffmpeg decodes on the CPU, two at most on a machine of 2 GB or less,
 * where each holds frames of its own; and ffmpeg niced by 5, so a decoder
 * late drops one frame of one layer rather than the painter late dropping
 * every layer's. */
static int machine_cores(void)
{
    int n = SDL_GetCPUCount();
    return n > 0 ? n : 1;
}

static void knobs_settle(void)
{
    int cores = machine_cores(), half = cores / 2 < 1 ? 1 : cores / 2 > 4 ? 4 : cores / 2;
    if (pool_want < 0)
        pool_want = half;
    if (decoder_soft < 0) {
        int ram = SDL_GetSystemRAM();
        decoder_soft = decoder_threads > 0 ? decoder_threads
                       : ram > 0 && ram <= 2048 && half > 2 ? 2 : half;
    }
    if (decoder_threads < 0)
        decoder_threads = 1;
    if (decoder_nice < 0)
        decoder_nice = 5;
}

/* One job done, wherever it is done. */
static void job_run(Job *j)
{
    double t0 = now_ms();
    if (pool.hook)
        pool.hook(j);
    if (j->kind == JOB_FRAME) {
        j->rgba = image_rgba(j->codec, j->data, j->len, &j->pw, &j->ph);
    } else {
        uint8_t *rgba = j->px;
        qoi_desc d = {(unsigned)j->w, (unsigned)j->h, 4, QOI_SRGB};
        if (j->yuv && (rgba = malloc((size_t)j->w * j->h * 4))) {
            int x, y;
            for (y = 0; y < j->h; y++)
                for (x = 0; x < j->w; x++) {
                    Uint32 c = yuv_argb(j->px, j->w, j->h, x, y);
                    uint8_t *o = rgba + ((size_t)y * j->w + x) * 4;
                    o[0] = (uint8_t)(c >> 16);
                    o[1] = (uint8_t)(c >> 8);
                    o[2] = (uint8_t)c;
                    o[3] = 255;
                }
        }
        if (rgba) {
            if (j->sum_it)
                j->sum = look_sum(rgba, (size_t)j->w * j->h * 4);
#ifdef BITWALL_JPEG
            if (j->quality > 0)
                j->q = jpeg_make(rgba, j->w, j->h, j->quality, &j->qlen);
            else
#endif
                j->q = qoi_encode(rgba, &d, &j->qlen);
        }
        if (rgba != j->px)
            free(rgba);
    }
    j->ms = now_ms() - t0;
}

/* A job finished, onto the done list, and the bell rung. */
static void job_finished(Job *j)
{
    j->next = NULL;
    SDL_LockMutex(bell.mu);
    if (pool.done_tail)
        pool.done_tail->next = j;
    else
        pool.done = j;
    pool.done_tail = j;
    SDL_CondBroadcast(bell.more);
    SDL_UnlockMutex(bell.mu);
}

static int pool_worker(void *arg)
{
    (void)arg;
    for (;;) {
        Job *j;
        SDL_LockMutex(pool.mu);
        while (!pool.head && !pool.stop)
            SDL_CondWait(pool.work, pool.mu);
        if (pool.stop) {
            SDL_UnlockMutex(pool.mu);
            return 0;
        }
        j = pool.head;
        pool.head = j->next;
        if (!pool.head)
            pool.tail = NULL;
        SDL_UnlockMutex(pool.mu);
        job_run(j);
        job_finished(j);
    }
}

/* The workers started, as many as the knob says and the machine allows;
 * once, the first time a job is handed over if not before. */
static void pool_start(void)
{
    int i;
    if (pool.started)
        return;
    pool.started = 1;
    knobs_settle();
    if (bell_open() < 0)
        return;
    pool.mu = SDL_CreateMutex();
    pool.work = SDL_CreateCond();
    if (!pool.mu || !pool.work)
        return;
    for (i = 0; i < pool_want && i < POOL_MAX; i++) {
        pool.th[i] = SDL_CreateThread(pool_worker, "worker", NULL);
        if (!pool.th[i])
            break;
        pool.size++;
    }
}

/* A job handed over: to a worker, or done here where there are none. */
static void pool_submit(Job *j)
{
    pool_start();
    pool.out++;
    j->next = NULL;
    if (!pool.size) {
        job_run(j);
        if (bell.mu)
            job_finished(j);
        return;
    }
    SDL_LockMutex(pool.mu);
    if (pool.tail)
        pool.tail->next = j;
    else
        pool.head = j;
    pool.tail = j;
    SDL_CondSignal(pool.work);
    SDL_UnlockMutex(pool.mu);
}

/* Every job finished since the last look, in the order they finished. */
static Job *pool_take(void)
{
    Job *j;
    if (!bell.mu)
        return NULL;
    SDL_LockMutex(bell.mu);
    j = pool.done;
    pool.done = pool.done_tail = NULL;
    SDL_UnlockMutex(bell.mu);
    return j;
}

/* Whether a job is waiting to be taken back; under the bell's lock. */
static int pool_ready(void)
{
    return pool.done != NULL;
}

static void job_free(Job *j)
{
    if (!j)
        return;
    free(j->data);
    free(j->px);
    free(j->rgba);
    free(j->q);
    free(j->say);
    free(j);
}

/* The workers stopped and waited for, and every job let go. */
static void pool_stop(void)
{
    int i;
    Job *j;
    if (!pool.size)
        return;
    SDL_LockMutex(pool.mu);
    pool.stop = 1;
    SDL_CondBroadcast(pool.work);
    SDL_UnlockMutex(pool.mu);
    for (i = 0; i < pool.size; i++)
        SDL_WaitThread(pool.th[i], NULL);
    pool.size = 0;
    while ((j = pool.head)) {
        pool.head = j->next;
        job_free(j);
    }
    pool.tail = NULL;
    for (j = pool_take(); j;) {
        Job *n = j->next;
        job_free(j);
        j = n;
    }
}
