/**
 * Optional instant-trigger webhook for the news bot.
 *
 * Telegram -> this Cloudflare Worker -> GitHub repository_dispatch -> pipeline.
 * Replaces the 1-3 hour polling latency with seconds. The Worker stays on
 * Cloudflare's free tier; it holds no state and runs no pipeline logic — it
 * just validates, answers the button tap instantly, and forwards the intent.
 *
 * Command coverage must stay in step with poll_telegram.py: every command the
 * poller answers, this forwards, and unknown commands become "help" — silence
 * after a command is indistinguishable from a dead bot.
 *
 * Required Worker secrets (wrangler secret put <NAME>):
 *   TELEGRAM_SECRET   the secret_token you register with setWebhook
 *   TELEGRAM_TOKEN    bot token, used to answer callbacks and report failures
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

    // A malformed body is not worth a 500: Telegram would retry it forever.
    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("ok", { status: 200 });
    }

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
    else if (text.startsWith("/auto")) payload = { action: "auto", text };
    // Typos happen (/new, /nwes...). Answer them the same way polling does.
    else if (text.startsWith("/")) payload = { action: "help" };

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
      let ok = false;
      let detail = "";
      try {
        const resp = await fetch(
          `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
          {
            method: "POST",
            headers: {
              "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
              "Accept": "application/vnd.github+json",
              "User-Agent": "news-bot-webhook",
            },
            body: JSON.stringify({ event_type: "telegram", client_payload: payload }),
          }
        );
        // GitHub answers 204 No Content on success.
        ok = resp.status === 204;
        if (!ok) detail = `HTTP ${resp.status} ${(await resp.text()).slice(0, 200)}`;
      } catch (err) {
        detail = String(err).slice(0, 200);
      }

      // A dropped dispatch would look exactly like a bot that ignored you.
      // Say so instead — an expired PAT is the likeliest cause and it is
      // invisible from the Telegram side otherwise.
      if (!ok) {
        console.error("repository_dispatch failed:", detail);
        await sendMessage(
          env,
          `⚠️ Couldn't reach GitHub to run that command.\n\n${detail}\n\n` +
          `The webhook's GITHUB_TOKEN has most likely expired or lost its ` +
          `Contents: write permission on the repo.`
        );
      }
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

async function sendMessage(env, text) {
  try {
    await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: env.OWNER_CHAT_ID, text }),
    });
  } catch (err) {
    console.error("failed to notify owner:", String(err));
  }
}
