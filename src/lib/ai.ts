const BASE = "https://api.deepseek.com/anthropic/v1/messages";
const MODEL = "deepseek-v4-flash";
const SYSTEM = [
  "你是 RWA Meme Radar（股票相关 Meme 关系雷达）的数据解说员，为页面生成一段简短解读。",
  "硬性规则：",
  "1. 只依据输入 JSON 里给出的字段解读；引用大数字时可四舍五入到便于阅读的精度（如 1615829362 写作约 16.2 亿），但不得改变数量级，不得外推或编造。",
  "2. 不预测价格涨跌，不给任何买卖建议，不用『可能上涨/值得买入』类措辞。",
  "3. 数据缺失就直接说缺失，不推测原因。",
  "4. 关系结论必须与 status 字段一致：verified 是已核验配对；仅有名称匹配是线索；无记录就是无关。",
  "5. 输出 120-180 字（英文 80-120 词），2-4 句，直接给结论，不写开场白和免责声明。",
  "6. 不要使用 Markdown 格式，输出纯文本。",
].join("\n");

type CacheEntry = { text: string; at: number; lang: string };
const cache = new Map<string, CacheEntry>();

export function aiEnabled() {
  return Boolean(process.env.DEEPSEEK_API_KEY);
}

export async function aiNarrate(key: string, lang: string, ttlMs: number, data: unknown, task: string, force = false): Promise<CacheEntry | null> {
  const hit = cache.get(key);
  if (!force && hit && hit.lang === lang && Date.now() - hit.at < ttlMs) return { ...hit, cached: true } as CacheEntry;
  const token = process.env.DEEPSEEK_API_KEY;
  if (!token) return null;
  const attempt = async (): Promise<{ text: string } | null> => {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 90000);
      const res = await fetch(BASE, {
        method: "POST",
        signal: controller.signal,
        headers: { "x-api-key": token, "anthropic-version": "2023-06-01", "content-type": "application/json" },
        body: JSON.stringify({
          model: MODEL,
          max_tokens: 8000,
          thinking: { type: "disabled" },
          system: SYSTEM,
          messages: [{ role: "user", content: `${lang === "en" ? `Write in English. ${task}` : `用简体中文输出。${task}`}\n\n数据 JSON：\n${JSON.stringify(data)}` }],
        }),
      });
      clearTimeout(timer);
      if (!res.ok) {
        console.error(`[ai] ${key}/${lang} HTTP ${res.status} ${(await res.text().catch(() => "")).slice(0, 150)}`);
        return null;
      }
      const json: any = await res.json();
      const text = (json.content ?? []).filter((b: any) => b.type === "text").map((b: any) => b.text).join("").trim();
      if (!text) { console.error(`[ai] ${key}/${lang} empty text (stop_reason=${json.stop_reason})`); return null; }
      if (lang === "en" && /\p{Script=Han}/u.test(text)) { console.error(`[ai] ${key}/${lang} leaked CJK characters`); return null; }
      return { text };
    } catch (e) {
      console.error(`[ai] ${key}/${lang} ${e instanceof Error ? e.message : e}`);
      return null;
    }
  };
  const result = await attempt() ?? (await new Promise(r => setTimeout(r, 3000)), await attempt());
  if (!result) return null;
  const entry = { text: result.text, at: Date.now(), lang };
  cache.set(key, entry);
  if (cache.size > 500) for (const k of cache.keys()) { cache.delete(k); if (cache.size <= 400) break; }
  return { ...entry, cached: false } as CacheEntry;
}
