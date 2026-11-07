/* Copyright (c) 2026 Nenad Micic
 * SPDX-License-Identifier: Apache-2.0 */
/* The actual bridge is copied under this name by the small existing-style
 * mutation driver. Private state checks supplement public replay; no DSP fork. */
#include "fmspike-profile.c"

static unsigned checks;
#define REQUIRE(x) do { checks++; if (!(x)) { fprintf(stderr,"PROFILE assertion line=%d: %s\n",__LINE__,#x); exit(1); } } while (0)
static const char *patch = "patch p 0 65536 140 0 0 256 2 900 120 250\npatch p 1 131072 0 2048 0 128 1 600 200 200\n";
static char *header(const char *extra)
{
    fms_space_t s; size_t n; char *t;
    fms_space_defaults(&s); s.field=FMS_SPACE_STEREO;
    n=fms_live_header_text(patch,"p",2,48000,&s,extra,NULL,0);
    t=malloc(n); REQUIRE(n && t);
    REQUIRE(fms_live_header_text(patch,"p",2,48000,&s,extra,t,n)==n); return t;
}
static fms_player_t *player(int profile)
{
    char err[FMS_ERR_LEN], *t=header(profile?"decodable\n":NULL);
    fms_player_t *p=fms_player_open_live(t,err,sizeof err);
    free(t); REQUIRE(p && !err[0]); return p;
}
static fms_host_t *host(fms_player_t *p)
{
    fms_host_t *h=fms_host_create(); REQUIRE(h);
    fms_host_swap(h,p); fms_host_settle(h); fms_host_set_playing(h,1); return h;
}
static void render(fms_host_t *h,unsigned frames)
{
    float a[64],b[64],*out[]={a,b}; REQUIRE(frames<=64);
    fms_host_render_f32(h,out,2,frames);
}
static fms_event_t position(unsigned voice,int x,int moving)
{
    fms_event_t e={0}; e.type=moving?FMS_EV_MOVE:FMS_EV_POS; e.voice=voice;
    e.x=x; e.y=1000; e.z=200; e.dur_ns=moving?1000000:0; return e;
}
static void push(fms_host_t *h,fms_event_t e) { REQUIRE(!fms_host_push(h,&e)); }
static void close_host(fms_host_t *h,fms_player_t *p)
{
    fms_host_record_stop_now(h); fms_host_swap(h,NULL); fms_host_settle(h);
    fms_host_destroy(h); fms_player_close(p);
}
static void replay(fms_host_t *h,const char *path,unsigned npos)
{
    char err[FMS_ERR_LEN],*text; FILE *f; size_t n; unsigned i,positions=0;
    uint64_t expected=fms_host_hash(h); fms_player_t *p;
    fms_host_record_stop_now(h); n=fms_host_take_text(h,NULL,0); REQUIRE(n);
    text=malloc(n); REQUIRE(text && fms_host_take_text(h,text,n)==n);
    REQUIRE(strstr(text,"\ndecodable\n"));
    for(i=0;i<h->nlog;i++) if(h->log[i].type==EV_POS) { REQUIRE(!h->log[i].t_ns);positions++; }
    REQUIRE(positions==npos);
    f=fopen(path,"w"); REQUIRE(f && fputs(text,f)>=0 && !fclose(f)); free(text);
    p=fms_player_open(&path,1,err,sizeof err); REQUIRE(p);
    REQUIRE(fms_player_render_all(p,7,NULL)==expected); fms_player_close(p);
}
static void lifecycle(const char *path)
{
    fms_player_t *p=player(1); fms_host_t *h=host(p); fms_event_t e;
    int32_t xyz[6],before[6]; uint64_t hash; unsigned nlog;
    fms_host_record_start(h); fms_host_set_playing(h,0);
    push(h,position(0,-700,0)); render(h,1); /* paused, actual frame remains zero */
    REQUIRE(fms_player_position(p)==0 && h->nlog==1 && !fms_host_profile_refusals(h));
    REQUIRE(fms_player_voice_pos(p,xyz,2)==2 && xyz[0]==-700);
    memcpy(before,xyz,sizeof before); hash=fms_player_hash(p);nlog=h->nlog;
    push(h,position(0,900,0)); push(h,position(1,900,1)); render(h,1);
    REQUIRE(fms_host_profile_refusals(h)==2 && h->nlog==nlog && fms_player_hash(p)==hash);
    REQUIRE(fms_player_voice_pos(p,xyz,2)==2 && !memcmp(xyz,before,sizeof xyz));
    push(h,position(1,800,0)); e=(fms_event_t){0};e.type=FMS_EV_ON;e.freq_millihz=261626;e.value=26000;push(h,e);
    fms_host_set_playing(h,1);render(h,64);
    REQUIRE(fms_player_position(p)==64 && h->nlog==3);
    fms_host_set_playing(h,0);hash=fms_player_hash(p);nlog=h->nlog;
    push(h,position(1,999,0));render(h,1);
    REQUIRE(fms_host_profile_refusals(h)==3 && h->nlog==nlog && fms_player_hash(p)==hash);
    REQUIRE(fms_player_voice_pos(p,xyz,2)==2 && xyz[0]==-700 && xyz[3]==800);
    replay(h,path,2);
    /* A second unseeded take resets the placement reservations. */
    fms_host_record_start(h);push(h,position(0,333,0));push(h,e);fms_host_set_playing(h,1);render(h,64);
    REQUIRE(fms_host_profile_refusals(h)==3 && h->nlog==2);
    replay(h,path,1);close_host(h,p);
}
static void seeds(const char *path)
{
    fms_player_t *p=player(1); fms_host_t *h; fms_event_t s[2],e;char err[FMS_ERR_LEN];int32_t xyz[6];
    s[0]=position(0,-100,0);s[1]=position(1,100,0);
    e=s[1];s[1].voice=0;
    REQUIRE(fms_player_seed_geometry(p,s,2,err,sizeof err)==1 && !p->seed_count);
    REQUIRE(fms_player_voice_pos(p,xyz,2)==2 && !xyz[0] && !xyz[3]);
    s[1]=e;REQUIRE(!fms_player_seed_geometry(p,s,2,err,sizeof err));
    h=host(p);fms_host_record_start(h);push(h,position(0,999,0));
    e=(fms_event_t){0};e.type=FMS_EV_ON;e.freq_millihz=261626;e.value=26000;push(h,e);render(h,64);
    REQUIRE(fms_host_profile_refusals(h)==1 && h->nlog==3);
    REQUIRE(fms_player_voice_pos(p,xyz,2)==2 && xyz[0]==-100 && xyz[3]==100);
    replay(h,path,2);
    fms_host_record_start(h);push(h,position(1,888,0));push(h,e);render(h,64);
    REQUIRE(fms_host_profile_refusals(h)==2 && h->nlog==3);replay(h,path,2);close_host(h,p);
    /* Non-profile users keep repeated/moving live placements. */
    p=player(0);h=host(p);push(h,position(0,-100,0));push(h,position(0,100,0));push(h,position(1,500,1));render(h,64);
    REQUIRE(!fms_host_profile_refusals(h));REQUIRE(fms_player_voice_pos(p,xyz,2)==2 && xyz[0]==100);
    close_host(h,p);
}
static char *read_file(const char *path)
{
    FILE *f=fopen(path,"rb");long n;char *s;REQUIRE(f && !fseek(f,0,SEEK_END));n=ftell(f);
    REQUIRE(n>=0 && !fseek(f,0,SEEK_SET));s=malloc((size_t)n+1);REQUIRE(s);
    REQUIRE(fread(s,1,(size_t)n,f)==(size_t)n && !fclose(f));s[n]=0;return s;
}
static void readers(const char *golden)
{
    char path[4096],err[FMS_ERR_LEN];const char *names[]={"limiter","move","shape"};unsigned i;
    fms_player_t *p;char *text,*h,*with_event;size_t n;tl_event_t e;
    for(i=0;i<3;i++) {
        snprintf(path,sizeof path,"%s/fx/bad/decodable-%s.tl",golden,names[i]);const char *one=path;
        p=fms_player_open(&one,1,err,sizeof err);REQUIRE(!p && strstr(err,"decodable: refused"));
        text=read_file(path);p=fms_player_open_live(text,err,sizeof err);free(text);
        REQUIRE(!p && strstr(err,"decodable: refused"));
    }
    h=header("decodable\n");n=strlen(h);with_event=malloc(n+128);REQUIRE(with_event);
    snprintf(with_event,n+128,"%s0 live 0 on 261626 26000\n",h);
    p=fms_player_open_live(with_event,err,sizeof err);REQUIRE(!p && strstr(err,"event-free"));
    free(h);free(with_event);
    /* Scope remains narrower than a complete timeline. */
    n=strlen(patch)+64;text=malloc(n);REQUIRE(text);snprintf(text,n,"%sdecodable\n",patch);
    REQUIRE(fms_patch_lines_check(text,strlen(text),48000,err,sizeof err)!=0 && strstr(err,"without record"));free(text);
    /* All profile offenders run through the real complete live reader. */
    for(i=0;i<7;i++) {
        const char *extra[]={"limiter 10 200\n", "softclip\n", "compressor 120 40 5 120 0\n",
            "chorus 15000 2500 800\n", "flanger 2000 1000 300 20000 0\n", "shape p 0 hard\n", "panlaw 32768\n"};
        char line[128];snprintf(line,sizeof line,"%sdecodable\n",extra[i]);h=header(line);
        p=fms_player_open_live(h,err,sizeof err);free(h);REQUIRE(!p && strstr(err,"decodable"));
    }
    h=header("flanger 2000 1000 0 20000 1\ngain -120\ndither 1\ndecodable\n");
    p=fms_player_open_live(h,err,sizeof err);free(h);REQUIRE(p && p->tl->decodable);fms_player_close(p);
    snprintf(path,sizeof path,"%s/fx/decodable.tl",golden);const char *one=path;
    p=fms_player_open(&one,1,err,sizeof err);REQUIRE(p && p->positioned==3);
    int16_t pcm[128];REQUIRE(fms_player_render(p,pcm,64)==64);
    fms_player_rewind(p);REQUIRE(p->positioned==3);fms_player_close(p);
    /* The const event validator cannot reserve a position during seed preflight. */
    p=player(1);fms_event_t a=position(0,100,0);REQUIRE(!fms_live_event(p,&a,&e) && !p->positioned);
    fms_player_close(p);
}

static void high_voice_and_rearm(void)
{
    fms_space_t sp;char err[FMS_ERR_LEN],*text;size_t n;fms_player_t *p,*next;fms_host_t *h;
    fms_space_defaults(&sp);sp.field=FMS_SPACE_STEREO;
    n=fms_live_header_text(patch,"p",64,48000,&sp,"decodable\n",NULL,0);text=malloc(n);REQUIRE(n && text);
    REQUIRE(fms_live_header_text(patch,"p",64,48000,&sp,"decodable\n",text,n)==n);
    p=fms_player_open_live(text,err,sizeof err);next=fms_player_open_live(text,err,sizeof err);free(text);REQUIRE(p && next);
    h=host(p);fms_host_set_playing(h,0);push(h,position(63,123,0));push(h,position(0,234,0));render(h,1);
    REQUIRE(p->positioned==(UINT64_C(1)<<63|1));
    push(h,position(63,999,0));push(h,position(64,999,0));render(h,1);
    REQUIRE(fms_host_profile_refusals(h)==1 && p->vpos[63][0]==123);
    REQUIRE(fms_host_swap(h,next)==p);fms_host_settle(h);REQUIRE(fms_host_retired(h));fms_player_close(p);
    push(h,position(63,345,0));render(h,1);
    REQUIRE(fms_host_profile_refusals(h)==1 && next->vpos[63][0]==345);close_host(h,next);
}
static void complete_patch_reader(void)
{
    char text[512],err[FMS_ERR_LEN];
    snprintf(text,sizeof text,"%sshape p 0 hard\ndecodable\n",patch);
    REQUIRE(fms_patch_lines_check(text,strlen(text),48000,err,sizeof err)!=0 && strstr(err,"decodable: refused"));
    printf("PROFILE_PATCH complete_reader=yes guard_injection=scope_only ok\n");
}

int main(int argc,char **argv)
{
    if(argc==4 && !strcmp(argv[3],"patch-complete")) { complete_patch_reader();return 0; }
    REQUIRE(argc==3); readers(argv[2]);lifecycle(argv[1]);seeds(argv[1]);high_voice_and_rearm();
    printf("PROFILE ok checks=%u callback_refusal=before_mutation rewind=yes seed=yes replay=yes\n",checks);return 0;
}
