/*
 * pickvedio-agent :: browser transcript extractor
 * opt/hatch/skills/video-summary/assets/browser-extract.js
 *
 * WHAT THIS IS
 * Run this ON the video page itself - a Bilibili video page or a YouTube watch
 * page - and it reads the caption data that page's own session is allowed to
 * see, then returns:
 *   {platform, url, title, durationSeconds, sourceKind, language, trackLabel,
 *    trackCount, segments: [{start, end, text}], charCount, warnings, reason}
 *
 * WHY IT EXISTS
 * This is the relay tier of the transcript ladder, and it is the tier that
 * actually works from a hosted agent:
 *   - Bilibili `player/v2` / `player/wbi/v2` answer an anonymous datacenter
 *     request with an EMPTY subtitle list (code 0, zero tracks) even after a
 *     buvid3 handshake; AI subtitles are served only to a logged-in session.
 *   - YouTube's watch page answers an anonymous datacenter request with HTTP
 *     429, and yt-dlp fails with "Sign in to confirm you're not a bot", so the
 *     caption tracks have to be read from inside a real browser page.
 * Inside the page both requests are the same ones the player itself makes, with
 * the session cookies already attached.
 *
 * HOW TO RUN IT
 *   1. CDP `Runtime.evaluate` (what Muse's browser does): pass this whole file
 *      as the expression with `returnByValue: true, awaitPromise: true`. The
 *      last statement is a Promise resolving to the result object.
 *   2. `Runtime.callFunctionOn`: the file installs `window.pickvedioExtract`, so
 *      call `pickvedioExtract({translateTo: "zh-CN"})` for a translated YouTube
 *      auto-caption track, or `pickvedioExtract({targetLangs: ["zh-CN"]})` to
 *      steer track selection, or `pickvedioExtract({maxSeconds: 3600})` to cap.
 *   3. DevTools console: paste the file, then
 *        copy(JSON.stringify(await pickvedioExtract({})))
 *      The resolved object is also kept in `window.__pvcRelay`.
 *
 * CONTRACT NOTES
 *   - `sourceKind` is one of up_subtitles | human_subtitles | platform_auto:
 *     the three tiers that come from real caption data. This script has no page
 *     description path on purpose. A description is not a transcript, and a
 *     summary built from one must never look like this script's output.
 *   - No captions, no session, blocked: `segments: []` plus a machine-readable
 *     `reason`. Empty is the correct answer; do not summarise the fallback.
 *   - No DRM, no paywall, no access-control bypass: it only reads caption
 *     tracks the already-authenticated page is itself permitted to read.
 *
 * FEEDING THE RESULT BACK  (see references/browser-extract.md)
 *   pvc push --url "<canonical url>" --transcript relay.json --style brief
 */
"use strict";

async function pickvedioExtract(options) {
  const opts = Object.assign(
    { translateTo: "", targetLangs: [], maxSeconds: 0 },
    options || {}
  );

  const result = {
    platform: "",
    url: location.href,
    title: "",
    durationSeconds: null,
    sourceKind: null,
    language: "",
    trackLabel: "",
    trackCount: 0,
    segments: [],
    charCount: 0,
    warnings: [],
    reason: "",
    extractedAt: new Date().toISOString(),
  };

  const warn = (message) => {
    const text = String(message || "").slice(0, 300);
    if (text && result.warnings.indexOf(text) < 0) result.warnings.push(text);
  };
  const num = (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const round3 = (value) => Math.round(Number(value) * 1000) / 1000;
  const videoDuration = () => {
    const node = document.querySelector("video");
    const seconds = node && num(node.duration);
    return seconds && seconds > 0 ? round3(seconds) : null;
  };

  function absolute(url, protocolBase) {
    if (!url) return "";
    const raw = String(url).trim();
    if (raw.indexOf("//") === 0) return (protocolBase || location.protocol) + raw;
    if (raw.indexOf("http://") === 0 || raw.indexOf("https://") === 0) return raw;
    return new URL(raw, location.origin).href;
  }

  async function getJson(url, init) {
    const response = await fetch(url, Object.assign({ credentials: "include" }, init || {}));
    const text = await response.text();
    if (!response.ok) {
      const error = new Error("http_" + response.status);
      error.status = response.status;
      error.snippet = text.slice(0, 160);
      throw error;
    }
    try {
      return JSON.parse(text);
    } catch (parseError) {
      const error = new Error("response_not_json");
      error.snippet = text.slice(0, 160);
      throw error;
    }
  }

  function pageLanguage() {
    const html = (document.documentElement && document.documentElement.lang) || "";
    const meta = document.querySelector('meta[property="og:locale"]');
    return String(html || (meta && meta.content) || navigator.language || "zh-CN").toLowerCase();
  }

  function titleFromDom() {
    const og = document.querySelector('meta[property="og:title"]');
    if (og && og.content) return String(og.content).trim();
    const heading = document.querySelector("h1");
    if (heading && heading.textContent) return String(heading.textContent).trim();
    return String(document.title || "")
      .split("_www.bilibili.com")[0]
      .split(" - YouTube")[0]
      .trim();
  }

  /**
   * `{start,end,text}` rows from whatever the platform gave us: tolerates
   * missing/inverted clocks, real newlines and the literal two-character
   * backslash-n that JSON3 embeds inside a single segment.
   */
  function normalize(rows) {
    const out = [];
    for (const row of rows) {
      if (!row) continue;
      let text = String(row.text == null ? "" : row.text);
      text = text.replace(/\\r/g, "").replace(/\r/g, "");
      text = text
        .split(/\n|\\n/)
        .map((part) => part.trim())
        .filter(Boolean)
        .join(" ");
      text = text.replace(/\s+/g, " ").trim();
      if (!text) continue;
      const start = Math.max(0, num(row.start) || 0);
      let end = num(row.end);
      if (end === null || end <= start) end = start + (num(row.duration) || num(row.dur) || 0);
      if (end <= start) end = start + 2;
      out.push({ start: round3(start), end: round3(end), text: text });
      if (opts.maxSeconds > 0 && start >= opts.maxSeconds) break;
    }
    for (let index = 1; index < out.length; index += 1) {
      if (out[index].start < out[index - 1].end) out[index - 1].end = round3(out[index].start);
      if (out[index].end < out[index].start) out[index].end = out[index].start;
    }
    return out;
  }

  /**
   * Caption body -> rows, whatever format YouTube answered with.
   *
   * JSON3 is preferred because it carries per-window timings, but a real watch
   * page can hand back XML (the default `timedtext` shape) or an empty body for
   * a forced format. Sniffing the bytes instead of assuming the request won is
   * what makes this survive the platform changing its default underneath.
   */
  function captionRows(text) {
    const body = String(text == null ? "" : text).replace(/^/, "");
    const head = body.trim().charAt(0);
    if (head === "{") {
      let payload = null;
      try {
        payload = JSON.parse(body);
      } catch (error) {
        return { format: "json3", rows: [], count: 0 };
      }
      const events = (payload && payload.events) || [];
      const rows = [];
      for (const event of events) {
        const segs = (event && event.segs) || [];
        if (!segs.length) continue; // window-events carry timing but no text
        rows.push({
          start: num(event.tStartMs) === null ? 0 : num(event.tStartMs) / 1000,
          duration: num(event.dDurationMs) === null ? 0 : num(event.dDurationMs) / 1000,
          text: segs.map((seg) => (seg && typeof seg.utf8 === "string" ? seg.utf8 : "")).join(""),
        });
      }
      return { format: "json3", rows: rows, count: events.length };
    }
    if (head === "<") {
      // srv1/srv3: <text start="1.0" dur="3.2">…</text>, entities and <font>/
      // <code>…</code> markup included, since a manual track styles rather than
      // speaks.
      const rows = [];
      const pattern = /<text\b([^>]*)>([\s\S]*?)<\/text>/g;
      let match;
      while ((match = pattern.exec(body)) !== null) {
        const attrs = match[1] || "";
        const start = num((attrs.match(/start=["']([^"']+)["']/) || [])[1]);
        const dur = num((attrs.match(/dur=["']([^"']+)["']/) || [])[1]);
        const raw = match[2]
          .replace(/<[^>]*>/g, " ")
          .replace(/&amp;/g, "&")
          .replace(/&lt;/g, "<")
          .replace(/&gt;/g, ">")
          .replace(/&quot;/g, '"')
          .replace(/&#39;|&apos;/g, "'")
          .replace(/&nbsp;/g, " ");
        rows.push({ start: start === null ? 0 : start, duration: dur === null ? 0 : dur, text: raw });
      }
      return { format: "srv3", rows: rows, count: rows.length };
    }
    return { format: "unknown", rows: [], count: 0 };
  }

  /**
   * Start muted playback so the player issues its own caption request.
   *
   * Both platforms bind caption bodies to an active playback session. Muted
   * play() is exempt from autoplay blocking, so this works in an automated
   * page without a user gesture. Returns a description of what happened, or
   * null when there was nothing to nudge -- and the caller must not pretend a
   * caption body arrived when it did not.
   */
  async function startPlayback(kind) {
    const video = document.querySelector("video");
    if (!video) {
      const button = document.querySelector(
        kind === "youtube"
          ? "button[aria-label*='播放'], button[aria-label*='Play'], ytd-thumbnail a#thumbnail"
          : ".bpx-player-ctrl-btn-play, .bilibili-player-video-btn"
      );
      if (!button) return null;
      button.click();
      await new Promise((resolve) => setTimeout(resolve, 1500));
      return "clicked-play-button";
    }
    const wasMuted = video.muted;
    try {
      video.muted = true;
      if (video.paused) await video.play();
    } catch (error) {
      video.muted = wasMuted;
      return null;
    }
    // Wait for real media time to advance: that is the signal the player has
    // begun requesting caption payloads, not merely that play() resolved.
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      if (video.currentTime > 0.5 && video.readyState >= 2) break;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    const moved = video.currentTime;
    if (!wasMuted) video.muted = true; // stay silent; restore nothing audible
    try {
      video.pause();
    } catch (error) {
      /* pausing is best-effort */
    }
    return moved > 0.5 ? "muted-playback-" + moved.toFixed(1) + "s" : null;
  }

  /**
   * Buffer caption bodies the page itself already received.
   *
   * A re-issued `timedtext` request can come back empty while the player's own
   * request for the same track succeeded: the response is bound to the fetch
   * that carried the player's attestation. Hooking fetch/XHR before the track
   * loads, then reading the buffer, is what lets an extraction run that starts
   * after playback recover the text instead of reporting nothing.
   */
  const captionBuffer = [];
  function installCaptionBuffer() {
    if (window.__pvcCaptionBuffered) return;
    window.__pvcCaptionBuffered = true;
    const looksLikeCaption = (u) => /timedtext|api\.youtube\.com\/api\/timedtext|subtitle_url|\/proxy\/www\.youtube\.com\/api\/timedtext/i.test(String(u || ""));
    const originalFetch = window.fetch;
    if (typeof originalFetch === "function") {
      window.fetch = function (input, init) {
        const url = typeof input === "string" ? input : (input && input.url) || "";
        return originalFetch.apply(this, arguments).then((response) => {
          if (looksLikeCaption(url)) {
            try {
              response
                .clone()
                .text()
                .then((text) => {
                  if (text && text.trim()) captionBuffer.push({ via: "fetch", url: url, text: text });
                })
                .catch(() => {});
            } catch (error) {
              /* a consumed body is simply not recorded */
            }
          }
          return response;
        });
      };
    }
    const OriginalOpen = XMLHttpRequest.prototype.open;
    const OriginalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__pvcUrl = String(url || "");
      return OriginalOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      const xhr = this;
      xhr.addEventListener("load", () => {
        try {
          if (looksLikeCaption(xhr.__pvcUrl) && xhr.responseText && xhr.responseText.trim()) {
            captionBuffer.push({ via: "xhr", url: xhr.__pvcUrl, text: xhr.responseText });
          }
        } catch (error) {
          /* some response types are unreadable; ignore */
        }
      });
      return OriginalSend.apply(this, arguments);
    };
  }

  function rowsFromBuffer() {
    for (const entry of captionBuffer) {
      const parsed = captionRows(entry.text);
      if (parsed.rows.length) {
        return Object.assign(parsed, { via: entry.via });
      }
    }
    return null;
  }

  /**
   * Ask the player, through its own control, to display subtitles.
   *
   * This is the same click a user makes; it does not reach into any internal
   * state and it cannot grant anything the session is not already allowed.
   */
  async function toggleCaptionsOn() {
    const selectors = [
      "button.ytp-subtitles-button",
      "[aria-label*='subtitles' i]",
      "[aria-label*='字幕' i]",
      "[aria-label*='cc' i]",
      ".bpx-player-ctrl-subtitle",
    ];
    for (const selector of selectors) {
      const button = document.querySelector(selector);
      if (!button) continue;
      const pressed = button.getAttribute && button.getAttribute("aria-pressed");
      if (pressed === "true") return "already-on:" + selector;
      try {
        button.click();
      } catch (error) {
        continue;
      }
      return "clicked:" + selector;
    }
    return null;
  }

  // ------------------------------------------------------------------ bilibili
  async function bilibili() {
    result.platform = "bilibili";
    const query = new URLSearchParams(location.search);
    // The player page's own state blob; name has changed across bilibili
    // rebuilds, so every known spelling is tried before the URL itself.
    const state =
      window.__INITIAL_PROFILE__ || window.__INITIAL_STATE__ || window.__INITIAL__INFO__ || null;
    const videoData = (state && (state.videoData || (state.ViewData && state.ViewData.videoInfo))) || null;
    const pathMatch = location.pathname.match(/\/video\/(BV[0-9A-Za-z]{10}|av\d+)/i);

    const bvid = String(
      (state && state.bvid) || (videoData && videoData.bvid) || (pathMatch && pathMatch[1]) || query.get("bvid") || ""
    );
    const aid = num((state && state.aid) || (videoData && videoData.aid) || query.get("aid") || "");
    let cid = num((state && state.cid) || (videoData && videoData.cid) || query.get("cid") || "");
    const part = Math.max(1, num(query.get("p")) || num(state && state.page && state.page.p) || 1);

    result.title = String((videoData && videoData.title) || (state && state.title) || titleFromDom());
    result.durationSeconds =
      num(videoData && videoData.duration) || num(state && state.duration) || videoDuration();

    // api.bilibili.com is what the web player calls; www.bilibili.com is kept
    // as a fallback in case the page's CSP/CORS setup dislikes the api host.
    const apiBases = ["https://api.bilibili.com", location.origin];

    async function apiGet(path, params) {
      let lastJson = null;
      let lastError = null;
      for (const base of apiBases) {
        try {
          const json = await getJson(base + path + "?" + params);
          if (json && json.code === 0) return json;
          lastJson = json || lastJson;
        } catch (error) {
          lastError = error;
        }
      }
      if (lastJson) return lastJson;
      throw lastError || new Error("no_response");
    }

    if (!bvid && !aid) {
      result.reason = "no_video_id";
      warn("Could not find a BV/av id on this page. Open the video page itself, not a list or search page.");
      return;
    }

    if (!cid) {
      // Resolve the part id through every documented endpoint, in order of how
      // little they demand. `pagelist` is the lightest and answers for a BV id
      // alone; `view` is what the page itself uses but has been observed to 404
      // when the session landed on a non-Chinese locale route. Returning on the
      // first failure turned a resolvable video into "view_api_failed".
      const resolvers = [
        {
          path: "/x/player/pagelist",
          params: (bvid ? "bvid=" + encodeURIComponent(bvid) : "aid=" + String(aid)) + "&platform=html5",
          pick: (payload) => {
            const pages = (payload && payload.data) || [];
            return { cid: num((pages[part - 1] || pages[0] || {}).cid), parts: pages.length };
          },
        },
        {
          path: "/x/web-interface/view",
          params: bvid ? "bvid=" + encodeURIComponent(bvid) : "aid=" + String(aid),
          pick: (payload) => {
            const data = (payload && payload.data) || {};
            const pages = data.pages || [];
            return {
              cid: num(data.cid) || num((pages[part - 1] || pages[0] || {}).cid),
              parts: pages.length,
              title: data.title,
              duration: num(data.duration),
            };
          },
        },
      ];
      const failures = [];
      for (const resolver of resolvers) {
        try {
          const payload = await apiGet(resolver.path, resolver.params);
          const found = resolver.pick(payload);
          if (found && found.cid) {
            cid = found.cid;
            if (found.title) result.title = String(found.title);
            if (found.duration) result.durationSeconds = found.duration;
            if (found.parts > 1) {
              warn("Multi-part video: using part " + part + " (cid " + cid + "). Other parts need their own page.");
            }
            break;
          }
          failures.push(resolver.path + ":no_cid");
        } catch (error) {
          failures.push(resolver.path + ":" + (error.status ? "http " + error.status : error.message));
        }
      }
      if (failures.length && !cid) warn("cid resolution attempts: " + failures.join(" | "));
    }

    if (!cid) {
      result.reason = "no_cid";
      warn(
        "No cid could be resolved from the page state, /x/player/pagelist or /x/web-interface/view, " +
          "so no subtitle track can be requested."
      );
      return;
    }

    const identity = bvid ? "bvid=" + encodeURIComponent(bvid) : "aid=" + String(aid);
    const playerParams = identity + "&cid=" + String(cid) + "&pn=1&ps=1";

    let player = null;
    for (const path of ["/x/player/v2", "/x/player/wbi/v2"]) {
      try {
        const json = await apiGet(path, playerParams);
        if (json && json.code === 0) {
          player = json;
          break;
        }
        warn(path + " returned code " + json.code + " " + String(json.message || "").slice(0, 80));
        player = player || json;
      } catch (error) {
        warn(path + " failed: " + (error.status ? "http " + error.status : error.message));
      }
    }

    const subtitles = (((player || {}).data || {}).subtitle || {}).subtitles || [];
    result.trackCount = subtitles.length;
    const usable = subtitles.filter((track) => track && track.subtitle_url);

    if (!usable.length) {
      // The verified anonymous failure mode: HTTP 200, code 0, zero tracks.
      result.reason = subtitles.length ? "subtitle_track_without_body" : "no_subtitles_for_session";
      if (!subtitles.length) {
        warn(
          "Bilibili served an empty subtitle list to this session. Either this account is not logged in " +
            "(AI subtitles require a logged-in session) or the video has no subtitles at all."
        );
      }
      return;
    }

    const lang = pageLanguage();
    const score = (track) => {
      const lan = String(track.lan || "").toLowerCase();
      let value = 0;
      if (!track.ai_type) value += 40; // creator-authored beats platform-generated
      if (lan === lang) value += 30;
      else if (lang.indexOf("-") > 0 && lan.indexOf(lang.split("-")[0]) === 0) value += 20;
      if (lan === "zh-cn" || lan === "zh-hans") value += 10;
      if (/-ai|-ai-/.test(lan) || /生成|AI/.test(String(track.lan_doc || ""))) value -= 5;
      return value;
    };
    usable.sort((left, right) => score(right) - score(left));
    const track = usable[0];

    let body;
    try {
      // `subtitle_url` is frequently protocol-relative ("//aisubtitle.hdslb.com/...").
      body = await getJson(absolute(track.subtitle_url));
    } catch (error) {
      result.reason = error.status === 403 ? "subtitle_body_forbidden" : "subtitle_body_failed";
      warn("subtitle body fetch failed: " + (error.status ? "http " + error.status : error.message));
      return;
    }

    const lines = (body && body.body) || [];
    result.lineCount = lines.length;
    result.segments = normalize(
      lines.map((line) => ({ start: line.from, end: line.to, text: line.content }))
    );
    result.language = String(track.lan || "");
    result.trackLabel = String(track.lan_doc || "");
    // ai_type is bilibili's own marker for a platform-generated track.
    result.sourceKind = track.ai_type ? "platform_auto" : "up_subtitles";
    if (!result.segments.length) result.reason = "subtitle_body_empty";
  }

  // ------------------------------------------------------------------ youtube
  async function youtube() {
    result.platform = "youtube";
    const playerResponse = window.ytInitialPlayerResponse || null;
    if (!playerResponse || typeof playerResponse !== "object") {
      result.reason = "no_player_response";
      warn("ytInitialPlayerResponse is absent. Open the watch page itself and wait for it to finish loading.");
      return;
    }

    const details = playerResponse.videoDetails || {};
    result.title = String(details.title || titleFromDom());
    result.durationSeconds = num(details.lengthSeconds) || videoDuration();

    const renderer = (playerResponse.captions || {}).playerCaptionsTracklistRenderer || {};
    const tracks = renderer.captionTracks || [];
    result.trackCount = tracks.length;

    const playability = playerResponse.playabilityStatus || {};
    // "OK" is the normal watch-page value; "ALLOWED" appears in other clients.
    const playable = ["", "OK", "ALLOWED"].indexOf(String(playability.status || "").toUpperCase()) >= 0;
    if (!playable) {
      warn("playabilityStatus=" + playability.status + (playability.reason ? " (" + playability.reason + ")" : ""));
    }

    if (!tracks.length) {
      result.reason = (renderer.translationLanguages || []).length ? "captions_translate_only" : "no_captions";
      if (!playable) result.reason = "not_playable";
      return;
    }

    const wanted = []
      .concat(String(renderer.defaultAudioLanguage || "").toLowerCase(), pageLanguage(), opts.targetLangs || [])
      .filter(Boolean);
    const pickFor = (list) => {
      for (const want of wanted) {
        const exact = list.filter((track) => String(track.languageCode || "").toLowerCase() === want)[0];
        if (exact) return exact;
        const stem = list.filter(
          (track) => String(track.languageCode || "").toLowerCase().indexOf(want.split("-")[0]) === 0
        )[0];
        if (stem) return stem;
      }
      return list[0] || null;
    };

    // A real caption track beats the platform's automatic speech recognition.
    const manual = tracks.filter((track) => track && track.kind !== "asr");
    const track = pickFor(manual) || pickFor(tracks);
    if (!track || !track.baseUrl) {
      result.reason = "caption_track_without_baseurl";
      return;
    }

    const isAsr = track.kind === "asr";
    result.sourceKind = isAsr ? "platform_auto" : "human_subtitles";
    result.language = String(track.languageCode || "");
    result.trackLabel = String(
      ((track.name || {}).simpleText) || (((track.name || {}).runs || [{}])[0] || {}).text || ""
    );
    result.trackKind = isAsr ? "asr" : "manual";

    // `baseUrl` carries a signed query bound to this page view, so it is reused
    // verbatim and only `fmt` is exchanged. Formats are tried in order:
    // json3 (structured), srv3 (XML with timings), then the untouched URL,
    // because YouTube answers the *default* format with XML and a forced
    // `fmt=json3` can come back empty for tracks the page itself plays fine --
    // that is the case that showed up against a real watch page.
    const root = absolute(track.baseUrl).replace(/([?&])fmt=[^&]*/, "");
    const withFmt = (fmt) => root + (root.indexOf("?") >= 0 ? "&" : "?") + "fmt=" + fmt;
    let candidates = [withFmt("json3"), withFmt("srv3"), root];
    if (opts.translateTo) {
      if (isAsr) {
        candidates = candidates.map((u) => u + "&tlang=" + encodeURIComponent(opts.translateTo));
        result.translatedTo = String(opts.translateTo);
      } else {
        warn("translateTo was ignored: only auto-generated (kind=asr) tracks can be machine-translated.");
      }
    }

    let rows = [];
    let eventsSeen = null;
    let lastError = null;
    const attemptFetch = async () => {
      const collected = [];
      for (const candidate of candidates) {
        let text;
        try {
          const response = await fetch(candidate, { credentials: "include" });
          text = await response.text();
          if (!response.ok) {
            lastError = { status: response.status };
            continue;
          }
        } catch (error) {
          lastError = { message: String(error && error.message) };
          continue;
        }
        if (!text || !text.trim()) {
          lastError = { message: "empty_body" };
          continue;
        }
        const parsed = captionRows(text);
        if (parsed.rows.length) {
          return { ok: true, parsed: parsed };
        }
        collected.push(parsed.format + ":0");
        lastError = { message: "no_rows_in_" + parsed.format };
      }
      return { ok: false, note: collected.join(",") };
    };

    let outcome = await rowsFromBuffer() ? { ok: true, parsed: rowsFromBuffer() } : await attemptFetch();
    if (!outcome.ok && !rowsFromBuffer()) {
      // The page may have fetched captions before this script was evaluated, or
      // after a player-side subtitle toggle. Turning captions on in the player
      // UI makes the *player* re-request the track, which the hook then sees.
      const toggled = await toggleCaptionsOn();
      if (toggled) {
        result.captionToggle = toggled;
        await new Promise((resolve) => setTimeout(resolve, 2500));
        const buffered = rowsFromBuffer();
        if (buffered) outcome = { ok: true, parsed: buffered };
      }
    }
    if (!outcome.ok && opts.nudge) {
      // YouTube lists caption tracks but serves an EMPTY timedtext body until the
      // player has actually started: the response is bound to the playback
      // session. Muted playback is allowed by every autoplay policy, so this
      // costs one second and turns "caption_body_failed" into real text. Bilibili
      // needs the same nudge for its AI subtitle request.
      const nudged = await startPlayback("youtube");
      if (nudged) {
        result.playbackNudge = nudged;
        outcome = await attemptFetch();
      }
    }
    if (outcome.ok) {
      rows = outcome.parsed.rows;
      eventsSeen = outcome.parsed.count;
      result.captionFormat = outcome.parsed.format;
      if (outcome.parsed.via) result.captionSource = outcome.parsed.via;
    } else {
      result.reason =
        lastError && lastError.status === 429
          ? "rate_limited"
          : lastError && (lastError.status === 401 || lastError.status === 403)
            ? "caption_forbidden"
            : "caption_body_failed";
      warn(
        "timedtext fetch failed: " +
          (lastError && (lastError.status ? "http " + lastError.status : lastError.message)) +
          (result.playbackNudge ? " (after starting muted playback)" : " (playback nudge unavailable)")
      );
      return;
    }

    result.eventCount = eventsSeen === null ? rows.length : eventsSeen;
    result.segments = normalize(rows);
    if (!result.segments.length) result.reason = "caption_body_empty";
  }

  // ------------------------------------------------------------------ dispatch
  const host = (location.hostname || "").toLowerCase();
  try {
    if (/(^|\.)bilibili\.com$/.test(host) || /(^|\.)b23\.tv$/.test(host)) {
      await bilibili();
    } else if (/(^|\.)youtube\.com$/.test(host) || /(^|\.)youtu\.be$/.test(host)) {
      await youtube();
    } else {
      result.platform = "other";
      result.reason = "unsupported_page";
      warn("This helper only extracts captions on Bilibili video pages and YouTube watch pages.");
    }
  } catch (error) {
    result.reason = result.reason || "extraction_failed";
    warn(
      (error && error.status ? "http_" + error.status + " " : "") + String((error && error.message) || error)
    );
  }

  if (result.segments.length) {
    result.charCount = result.segments.reduce((total, seg) => total + seg.text.length, 0);
    if (result.durationSeconds === null) {
      result.durationSeconds = round3(result.segments[result.segments.length - 1].end);
    }
  } else if (!result.reason) {
    result.reason = "empty_segments";
  }

  return result;
}

if (typeof window !== "undefined") {
  window.pickvedioExtract = pickvedioExtract;
}

// Evaluating this file returns the result object; humans can also read
// window.__pvcRelay or re-run it with options.
pickvedioExtract({}).then((json) => {
  window.__pvcRelay = json;
  console.log(JSON.stringify(json));
  return json;
});
