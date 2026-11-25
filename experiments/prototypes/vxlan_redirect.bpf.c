/* SPDX-License-Identifier: GPL-2.0
 * Isolated lab only: one static IPv4 route per ingress attachment.
 * Minimal stable Linux UAPI declarations avoid a libbpf/header dependency.
 * Helper IDs and context len offset: Linux include/uapi/linux/bpf.h.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef unsigned long long u64;
#define SEC(s) __attribute__((section(s), used))
#define UINT(n, v) int (*n)[v]
#define TYPE(n, v) v *n
struct __sk_buff { u32 len; };
struct route {
    u32 network, mask, output;
    u8 source_mac[6], destination_mac[6];
    u32 mtu;
    u64 redirected, fallback, dropped;
};
_Static_assert(sizeof(struct route) == 56, "controller ABI");
struct {
    UINT(type, 2); /* BPF_MAP_TYPE_ARRAY */
    UINT(max_entries, 1);
    TYPE(key, u32);
    TYPE(value, struct route);
} forwarding SEC(".maps");
static void *(*lookup)(void *, const void *) = (void *)1;
static long (*store)(struct __sk_buff *, u32, const void *, u32, u64) = (void *)9;
static long (*csum_replace)(struct __sk_buff *, u32, u64, u64, u64) = (void *)10;
static long (*redirect)(u32, u64) = (void *)23;
static long (*load)(struct __sk_buff *, u32, void *, u32) = (void *)26;

SEC("classifier")
int shortcut(struct __sk_buff *skb)
{
    u32 key = 0;
    struct route *r = lookup(&forwarding, &key);
    if (!r || !r->output) return 0; /* TC_ACT_OK */
    u8 h[34];
    if (load(skb, 0, h, sizeof(h))) goto fallback;
    /* Untagged Ethernet / IPv4, no IP options or fragments. */
    if (h[12] != 8 || h[13] != 0 || h[14] != 0x45) goto fallback;
    if ((h[20] & 0x3f) || h[21]) goto fallback;
    u32 dst = ((u32)h[30]<<24) | ((u32)h[31]<<16) | ((u32)h[32]<<8) | h[33];
    if ((dst & r->mask) != r->network) goto fallback;
    u32 length = ((u32)h[16]<<8) | h[17];
    if (length < 20 || length > r->mtu || length + 14 > skb->len) goto fallback;
    /* No ICMP time-exceeded generator in this proof of concept. */
    if (h[22] <= 1) goto drop;
    u16 old = __builtin_bswap16(((u16)h[22]<<8) | h[23]);
    u16 next = __builtin_bswap16(((u16)(h[22]-1)<<8) | h[23]);
    u8 ttl = h[22]-1;
    /* Incremental IPv4 checksum; helper offsets refer to Ethernet frame. */
    if (csum_replace(skb, 24, old, next, 2) ||
        store(skb, 22, &ttl, 1, 0) ||
        store(skb, 0, r->destination_mac, 6, 0) ||
        store(skb, 6, r->source_mac, 6, 0)) goto drop;
    __sync_fetch_and_add(&r->redirected, 1);
    return redirect(r->output, 0); /* target egress; Linux VXLAN still runs */
fallback:
    __sync_fetch_and_add(&r->fallback, 1);
    return 0;
drop:
    __sync_fetch_and_add(&r->dropped, 1);
    return 2; /* TC_ACT_SHOT */
}
char program_license[] SEC("license") = "GPL";
