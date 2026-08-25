/* Shared likes module: token storage + GitHub Contents API + like-button
   wiring. Loaded on every page that needs likes (player pages, likes.html).
   Reuses the SAME token storage key as settings.html ("digestToken"), so
   connecting once on either page works everywhere on the site -- likes are
   stored centrally in docs/likes.json via the repo, not in this browser's
   localStorage, specifically so they sync across devices. */
window.DigestLikes = (function () {
  "use strict";
  var OWNER = "joor241", REPO = "discogs-genre-digest";
  var API = "https://api.github.com/repos/" + OWNER + "/" + REPO;
  var TOKEN_KEY = "digestToken";
  var BASE_URL = window.DIGEST_BASE_URL || "";

  function getToken() {
    return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || "";
  }

  function ghFetch(path, opts) {
    opts = opts || {};
    var token = getToken();
    var headers = Object.assign({
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    }, opts.headers || {});
    if (token) headers["Authorization"] = "Bearer " + token;
    return fetch(API + path, Object.assign({}, opts, { headers: headers }));
  }

  // UTF-8-safe base64 helpers -- plain atob/btoa only handle Latin1, and
  // titles/descriptions here are frequently not (e.g. "Premià Del Mar").
  function b64encode(str) { return btoa(unescape(encodeURIComponent(str))); }
  function b64decode(str) { return decodeURIComponent(escape(atob(str.replace(/\n/g, "")))); }

  // Reading is always a plain fetch of the public file straight off Pages --
  // no token needed, so "is this liked" is visible on any device, even one
  // that has never connected. Only toggling a like needs a token.
  function fetchLikes() {
    var url = (BASE_URL ? BASE_URL.replace(/\/$/, "") : ".") + "/likes.json";
    return fetch(url, { cache: "no-store" })
      .then(function (res) { return res.ok ? res.json() : {}; })
      .catch(function () { return {}; });
  }

  // Retries once on a 409 (another device wrote likes.json between this
  // read and this write) by re-reading and re-applying the same mutation --
  // same principle as the publish workflow's own -X ours race fix, just
  // done client-side here since there's no git working copy to merge.
  function mutateLikes(mutator, attempt) {
    attempt = attempt || 0;
    if (!getToken()) return Promise.reject(new Error("Not connected"));
    return ghFetch("/contents/docs/likes.json").then(function (res) {
      if (!res.ok) throw new Error("Could not read likes.json: HTTP " + res.status);
      return res.json();
    }).then(function (file) {
      var current;
      try { current = JSON.parse(b64decode(file.content)); } catch (e) { current = {}; }
      var updated = mutator(current);
      var body = {
        message: "Update likes",
        content: b64encode(JSON.stringify(updated, null, 2)),
        sha: file.sha,
      };
      return ghFetch("/contents/docs/likes.json", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function (putRes) {
        if (putRes.status === 409 && attempt < 2) return mutateLikes(mutator, attempt + 1);
        if (!putRes.ok) {
          return putRes.text().then(function (t) {
            throw new Error("Saving likes failed: HTTP " + putRes.status +
              (t ? " - " + t.slice(0, 180) : ""));
          });
        }
        return updated;
      });
    });
  }

  function likeItem(payload) {
    return mutateLikes(function (likes) {
      var copy = Object.assign({}, likes);
      copy[payload.url] = Object.assign({}, payload, { liked_at: new Date().toISOString() });
      return copy;
    });
  }

  function unlikeItem(url) {
    return mutateLikes(function (likes) {
      var copy = Object.assign({}, likes);
      delete copy[url];
      return copy;
    });
  }

  // --- Auto-wire any .likebtn[data-like-key] on the page ---

  function paintHeart(btn, liked) {
    btn.textContent = liked ? "♥" : "♡";
    btn.classList.toggle("liked", liked);
    btn.setAttribute("aria-pressed", liked ? "true" : "false");
  }

  function connectHref() {
    return (BASE_URL ? BASE_URL.replace(/\/$/, "") + "/" : "") + "settings.html";
  }

  function initLikeButtons() {
    var buttons = document.querySelectorAll(".likebtn[data-like-key]");
    if (!buttons.length) return;

    fetchLikes().then(function (likes) {
      buttons.forEach(function (btn) {
        paintHeart(btn, !!likes[btn.getAttribute("data-like-key")]);
      });
    });

    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest ? ev.target.closest(".likebtn[data-like-key]") : null;
      if (!btn) return;
      ev.preventDefault();

      if (!getToken()) {
        btn.title = "Connect on the Settings page to save likes across your devices";
        window.open(connectHref(), "_blank", "noopener");
        return;
      }

      var key = btn.getAttribute("data-like-key");
      var wasLiked = btn.classList.contains("liked");
      paintHeart(btn, !wasLiked); // optimistic
      btn.disabled = true;

      var action = wasLiked
        ? unlikeItem(key)
        : likeItem(JSON.parse(btn.getAttribute("data-like-payload") || "{}"));

      action.catch(function (err) {
        paintHeart(btn, wasLiked); // revert
        var msg = err.message || String(err);
        btn.title = "Could not save: " + msg;
        // A silent tooltip is easy to miss entirely -- the heart just seems
        // to snap back with no visible explanation, which reads as "liking
        // doesn't work" rather than "here is specifically why it failed".
        // A 403 here almost always means the connected token predates the
        // Contents permission this feature needs (any token made before
        // likes existed), not a fresh problem each time -- worth saying
        // outright rather than making every user re-diagnose the same thing.
        var friendly = /HTTP 403/.test(msg) || /Resource not accessible/i.test(msg)
          ? "Could not save your like: this token doesn't have permission to write likes.\n\n" +
            "If you connected before the Likes feature existed, your token predates the " +
            "\"Contents\" permission it needs. Go to Settings, forget the token, and generate " +
            "a new one with Contents: Read and write enabled."
          : "Could not save your like: " + msg;
        alert(friendly);
      }).then(function () {
        btn.disabled = false;
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initLikeButtons);
  } else {
    initLikeButtons();
  }

  return {
    getToken: getToken,
    fetchLikes: fetchLikes,
    likeItem: likeItem,
    unlikeItem: unlikeItem,
  };
})();
