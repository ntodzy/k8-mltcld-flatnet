/* SPDX-License-Identifier: GPL-2.0
 * Benchmark TCP flow only. Preserve Ethernet and TTL: this replaces L2 bridge
 * forwarding, not an IP routing hop. Linux VXLAN and guest Cilium remain.
 */
typedef unsigned char u8;
typedef unsigned int u32;
typedef unsigned long long u64;
#define SEC(s) __attribute__((section(s), used))
#define UINT(n,v) int (*n)[v]
#define TYPE(n,v) v *n
struct __sk_buff { u32 len; };
struct flow { u32 source, destination; };
struct target { u32 output, bypass; u64 matched, redirected, passed; };
_Static_assert(sizeof(struct target) == 32, "controller ABI");
struct {
    UINT(type, 1); /* BPF_MAP_TYPE_HASH */
    UINT(max_entries, 1);
    TYPE(key, struct flow);
    TYPE(value, struct target);
} benchmark_flow SEC(".maps");
static void *(*lookup)(void *, const void *) = (void *)1;
static long (*load)(struct __sk_buff *, u32, void *, u32) = (void *)26;
static long (*redirect)(u32,u64) = (void *)23;
SEC("classifier")
int bridge_shortcut(struct __sk_buff *skb)
{
    u8 h[34];
    if (load(skb, 0, h, 34)) return 0;
    if (h[12]!=8 || h[13]!=0 || h[14]!=0x45 || h[23]!=6) return 0;
    if ((h[20]&0x3f) || h[21]) return 0;
    struct flow k = {
        ((u32)h[26]<<24)|((u32)h[27]<<16)|((u32)h[28]<<8)|h[29],
        ((u32)h[30]<<24)|((u32)h[31]<<16)|((u32)h[32]<<8)|h[33]
    };
    struct target *t = lookup(&benchmark_flow, &k);
    if (!t) return 0;
    __sync_fetch_and_add(&t->matched,1);
    if (!t->bypass || !t->output) {
        __sync_fetch_and_add(&t->passed,1);
        return 0;
    }
    __sync_fetch_and_add(&t->redirected,1);
    return redirect(t->output,0);
}
char program_license[] SEC("license") = "GPL";
