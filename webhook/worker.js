/**
 * Optional instant-trigger webhook for the news bot.
 *
 * Telegram -> this Cloudflare Worker -> GitHub repository_dispatch -> pipeline.
 * Replaces the 5-20 minute polling latency with seconds. The Worker stays on
 * Cloudflare's free tier; it holds no state and runs no pipeline logic — it
 * just validates, answers the button tap instantly, and forwards the intent.
 *
 * Required Worker secrets (wrangler secret put <NAME>):
 *   TELEGRAM_SECRET   the secret_token you register with setWebhook
 *   TELEGRAM_TOKEN    bot token, used only to answer callback queries fast
 *   OWNER_CHAT_ID     your chat id; everyone else is ignored
 *   GITHUB_TOKEN      fine-grained PAT for this repo with Contents: write
 *   GITHUB_REPO       e.g. "nurbekkmu/daily-ai-news-bot"
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("ok", { status: 200 });
    }
    // Telegram echoes back the secret_token registered with setWebhook —
    // reject anything that isn't really Telegram.
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    const update = await request.json();
    const message = update.message;
    const callback = update.callback_query;

    const chatId = String(
      message?.chat?.id ?? callback?.message?.chat?.id ?? ""
    );
    if (chatId !== env.OWNER_CHAT_ID) {
      return new Response("ok", { status: 200 }); // ignore strangers, but 200 so Telegram stops retrying
    }

    let payload = null;

    const text = (message?.text ?? "").trim();
    if (text === "/news") payload = { action: "news" };
    else if (text === "/weekly") payload = { action: "weekly" };
    else if (text === "/stats") payload = { action: "stats" };
    else if (text.startsWith("/topics")) payload = { action: "topics", text };

    if (callback) {
      const data = callback.data ?? "";
      if (data === "news_command") {
        payload = { action: "news" };
        await answerCallback(env, callback.id, "Getting latest news...");
      } else if (data.startsWith("fb:")) {
        const [, verdict, hash] = data.split(":");
        payload = { action: "feedback", verdict, hash };
        await answerCallback(
          env, callback.id,
          verdict === "up" ? "Noted 👍 — more like this" : "Noted 👎 — less like this"
        );
      }
    }

    if (payload) {
      await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
          "Accept": "application/vnd.github+json",
          "User-Agent": "news-bot-webhook",
        },
        body: JSON.stringify({ event_type: "telegram", client_payload: payload }),
      });
    }

    return new Response("ok", { status: 200 });
  },
};

async function answerCallback(env, callbackQueryId, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/answerCallbackQuery`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ callback_query_id: callbackQueryId, text }),
  });
}
