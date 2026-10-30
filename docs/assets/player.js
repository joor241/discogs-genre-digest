
(function () {
  // Substituted at render time from the ICON_* constants, so the markup
  // and the JS that swaps it can never drift out of sync.
  var ICON_PLAY = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true" focusable="false"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>';
  var ICON_PAUSE = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true" focusable="false"><path fill="currentColor" d="M6 5h3.5v14H6zM14.5 5H18v14h-3.5z"/></svg>';
  var ICON_PREV = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" focusable="false"><path fill="currentColor" d="M6 6h2.5v12H6zm3.5 6L18 6v12z"/></svg>';
  var ICON_NEXT = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" focusable="false"><path fill="currentColor" d="M15.5 6H18v12h-2.5zM6 6l8.5 6L6 18z"/></svg>';

  // Two hidden YouTube players, not one: ytPlayer is the one you hear,
  // ytSpare sits muted behind it and fetches ahead. See "fetching ahead".
  var ytPlayer = null, ytSpare = null;
  var ytApiReady = false;   // iframe_api has loaded
  var ytUp = false;         // ytPlayer has fired onReady
  var ytSpareUp = false;    // so has ytSpare
  var ytQueued = null;      // a press that arrived before ytUp
  var spareBar = null, spareVideoId = null, spareStart = 0, spareAt = 0;
  var spareHeld = false;    // its fetch got as far as playing, and was paused there
  var swapPending = false, swapTimer = null, nearEndDone = false;
  var hoverTimer = null, hoverBar = null, hoverAt = 0, hoverEnterAt = 0, hoverFetchAt = 0;
  // Fetching ahead costs a second request per track. Off for anyone who
  // asked their browser to save data, and ?ahead=0 turns it off by hand --
  // the player then behaves exactly like the single-player one it replaced.
  var FETCH_AHEAD = !/[?&]ahead=0/.test(location.search) &&
    !(navigator.connection && navigator.connection.saveData);
  // Preloading everything (see "preloading everything") is for the digest
  // page, which ships the element that shows its progress. likes.html does
  // not, and keeps fetching one track ahead.
  var preloadEl = document.getElementById('preload');
  var PRELOAD_ALL = FETCH_AHEAD && !!preloadEl;
  var ytMain = null;        // the player cold loads go into; see playYtFrom
  // audioBase plays any MP3 that was not preloaded; audioEl is whichever
  // <audio> is the one you hear, audioBase or a preloaded one.
  var audioBase = new Audio();
  audioBase.preload = 'none';
  var audioEl = audioBase;

  var activeEngine = null;  // 'yt' | 'audio' | null
  var activeBar = null, activeVideoId = null, activeSrc = null;
  var steppedTo = null;     // see playFrom

  // One console line per press: how the track was loaded and how long it
  // took to sound. Open the browser console (F12) on a slow track to see
  // which way it went. Over 1.5s is a warning.
  var pressLog = null;
  function logHow(how) { if (pressLog) pressLog.how = how; }
  // at: where in the track it started, in seconds, for a press that
  // started somewhere along the bar rather than at the beginning.
  function logSound(at) {
    if (!pressLog || pressLog.done) return;
    pressLog.done = true;
    var ms = Math.round(performance.now() - pressLog.t);
    var where = pressLog.fromBar && isFinite(at) ? ' at ' + fmt(at) : '';
    (ms > 1500 ? console.warn : console.info)(
      '[digest] ' + ms + ' ms to sound' + where + ' \u00b7 ' + pressLog.how +
      ' \u00b7 ' + pressLog.name);
  }
  function ytNow() {
    try { return ytPlayer.getCurrentTime(); } catch (err) { return NaN; }
  }
  function logWarn(text) {
    console.warn('[digest] ' + text + (pressLog ? ' \u00b7 ' + pressLog.name : ''));
  }
  var pendingSeekFraction = null;
  var pollTimer = null;

  function fmt(t) {
    if (!isFinite(t) || t < 0) t = 0;
    var m = Math.floor(t / 60), s = Math.floor(t % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function row(bar) { return bar.closest('.trow'); }

  function paint(bar, fraction, curLabel, durLabel, state) {
    var fillEl = bar.querySelector('.fill');
    if (fillEl) fillEl.style.width = (Math.max(0, Math.min(1, fraction)) * 100) + '%';
    bar.setAttribute('aria-valuenow', Math.round(fraction * 100));
    var r = row(bar);
    if (!r) return;
    r.classList.toggle('playing', state === 'playing');
    var t = r.querySelector('.time');
    if (t) t.textContent = curLabel + ' / ' + durLabel;
    var btn = r.querySelector('.ppbtn');
    if (btn) {
      btn.classList.toggle('loading', state === 'loading');
      btn.innerHTML = state === 'playing' ? ICON_PAUSE : ICON_PLAY;
    }
    // Keep the bottom transport in step with the row: same play/pause icon,
    // same progress, same clock.
    var nbToggle = document.querySelector('#nowbar [data-toggle]');
    if (nbToggle) {
      nbToggle.innerHTML = state === 'playing' ? ICON_PAUSE : ICON_PLAY;
    }
    if (bar === activeBar) {
      var nbFill = document.querySelector('#nowbar .nbfill');
      if (nbFill) nbFill.style.width = (Math.max(0, Math.min(1, fraction)) * 100) + '%';
      var nbSeek = document.querySelector('#nowbar .nbseek');
      if (nbSeek) nbSeek.setAttribute('aria-valuenow', Math.round(fraction * 100));
      var nbTime = document.querySelector('#nowbar .nbtime');
      if (nbTime) nbTime.textContent = curLabel + ' / ' + durLabel;
    }
  }

  function knownDuration(bar) {
    return parseInt(bar.getAttribute('data-dur'), 10) || 0;
  }

  function durLabel(bar) {
    var known = knownDuration(bar);
    return known ? fmt(known) : '--:--';
  }

  function resetBar(bar) {
    if (!bar) return;
    paint(bar, 0, '0:00', durLabel(bar), 'idle');
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // ---- YouTube backend (Discogs releases) ----

  function pollYt() {
    if (!ytPlayer || !activeBar) return;
    var dur = 0, cur = 0, state = -1;
    try {
      dur = ytPlayer.getDuration() || 0;
      cur = ytPlayer.getCurrentTime() || 0;
      state = ytPlayer.getPlayerState();
    } catch (err) { return; }
    var justSeeked = false;
    if (pendingSeekFraction !== null && dur > 0) {
      var target = pendingSeekFraction; pendingSeekFraction = null;
      ytPlayer.seekTo(target * dur, true);
      cur = target * dur;
      justSeeked = true;
      if (pressLog && !pressLog.done) pressLog.seekTo = cur;
    }
    // A jump within the playing track that never stopped to buffer gives
    // onYtState nothing to report; this sees it arrive instead.
    if (!justSeeked && pressLog && !pressLog.done && pressLog.seekTo !== undefined &&
        state === 1 && Math.abs(cur - pressLog.seekTo) < 3) {
      logSound(cur);
    }
    // Hovering another row borrows the spare, so by the end of a track it
    // may no longer hold the one auto-advance is about to ask for.
    if (!nearEndDone && state === 1 && dur > 0 && dur - cur < 20) {
      nearEndDone = true;
      preloadNext();
    }
    var effectiveDur = dur || knownDuration(activeBar);
    var frac = effectiveDur > 0 ? cur / effectiveDur : 0;
    paint(activeBar, frac, fmt(cur), dur > 0 ? fmt(dur) : durLabel(activeBar),
          state === 1 ? 'playing' : (state === 3 ? 'loading' : 'paused'));
  }

  function buildYt(host, onReady) {
    return new YT.Player(host, {
      height: '113', width: '200',
      playerVars: { playsinline: 1, controls: 0, disablekb: 1, rel: 0, modestbranding: 1 },
      events: {
        onReady: onReady, onStateChange: onYtState, onError: onYtError,
        onAutoplayBlocked: onYtBlocked
      }
    });
  }

  // A tab opened in the background is not allowed to start anything, muted
  // or not, so the spare's fetch never happens there. Forget it, or the
  // first press would sit waiting on it; visibilitychange asks again.
  function onYtBlocked(ev) {
    if (ev.target === ytPlayer) return;
    if (ev.target.__pool) { poolBlocked(ev.target.__pool); return; }
    spareBar = null; spareVideoId = null;
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    everShown = true; hiddenRefused = false;
    preloadNext();
    pumpPreload();
  });

  // Which video a player says it has, or '' when it does not say.
  function ytVideoId(player) {
    try { return player.getVideoData().video_id || ''; } catch (err) { return ''; }
  }

  function onYtState(ev) {
    // A preloaded player that got as far as playing has its video; it
    // counts as loaded whether it is held below or is already the one
    // you hear (pressed while still on its way).
    var entry = ev.target.__pool;
    // Not a word from the video a reclaimed player held before.
    if (entry && ev.data === 1 && ytVideoId(ev.target) && ytVideoId(ev.target) !== entry.key) {
      return;
    }
    if (entry && ev.data === 1) { entry.held = true; poolSettle(entry, 'loaded'); }
    if (ev.target !== ytPlayer) {
      if (entry) {
        if (ev.data === 1) { try { ev.target.pauseVideo(); } catch (err) {} }
        return;
      }
      // The spare is there to fetch, never to be heard: hold it the moment
      // it actually starts. Not earlier -- pausing a load that is still on
      // its way throws the head start away (measured: paused 150ms in, the
      // press then waited as long as a cold one).
      if (ev.data === 1) {
        // Unless this is the previous track's last word, arriving after
        // the player was handed something new to fetch.
        if (!ytVideoId(ev.target) || ytVideoId(ev.target) === spareVideoId) spareHeld = true;
        try { ev.target.pauseVideo(); } catch (err) {}
      }
      return;
    }
    if (activeEngine !== 'yt' || !activeBar) return;
    if (ev.data === 1 || ev.data === 3) swapPending = false;
    if (ev.data === 1) {
      // Not while a seek is still to come (length unknown until now):
      // pollYt times that one once it lands.
      if (pendingSeekFraction === null) logSound(ytNow());
      preloadNext();
    } else if (ev.data === YT.PlayerState.ENDED) {
      var d = ytPlayer.getDuration() || knownDuration(activeBar);
      paint(activeBar, 1, fmt(d), fmt(d), 'paused');
      stopPolling();
      step(1);  // roll on to the next track, top to bottom
    }
  }

  function onYtError(ev) {
    if (ev.target !== ytPlayer && ev.target.__pool) {
      // Same as the spare below: a dead video found while preloading greys
      // its rows out now, so a press or auto-advance never stops on it.
      var pooled = ev.target.__pool;
      var gone = ytVideoId(ev.target);
      if (gone && gone !== pooled.key) return;  // about the video it held before
      if (ev.data === 100 || ev.data === 101 || ev.data === 150) {
        markErrored('data-yt', pooled.key);
      }
      poolSettle(pooled, 'failed');
      return;
    }
    if (ev.target !== ytPlayer) {
      // Found out while fetching ahead that this one cannot play here
      // (removed, or its owner switched embedding off). Grey the row out
      // now rather than on the press, and line up the one after it, so
      // auto-advance steps past a dead video instead of stopping on it.
      // Unless it is about a fetch that has since been replaced: that must
      // not grey out the row that replaced it.
      var dead = ev.data === 100 || ev.data === 101 || ev.data === 150;
      var failedId = ytVideoId(ev.target);
      if (failedId && failedId !== spareVideoId) return;
      var r = spareBar ? row(spareBar) : null;
      spareBar = null; spareVideoId = null;
      if (dead && r) { r.classList.add('errored'); preloadNext(); }
      return;
    }
    if (activeEngine === 'yt' && activeBar) {
      var ar = row(activeBar);
      if (ar) ar.classList.add('errored');
      logWarn('YouTube cannot play this video (error ' + ev.data + ')' +
              (activeBar === steppedTo ? ', skipping to the next' : ''));
      if (pressLog) pressLog.done = true;
      // Listening top to bottom: one dead video must not stop the run.
      if (activeBar === steppedTo) step(1);
    }
  }

  function ensureYt(cb) {
    if (ytUp) { if (cb) cb(); return; }
    if (cb) ytQueued = cb;  // the latest press wins
    if (ytPlayer || !ytApiReady) return;
    var host = document.getElementById('yt-audio-host');
    if (!host) return;
    ytPlayer = ytMain = buildYt(host, function () {
      ytUp = true;
      var queued = ytQueued; ytQueued = null;
      if (queued) queued();
      buildSpare();
      // Only now: the player a press needs first is standing, so building
      // the rest cannot hold that up.
      ytPoolStarted = true;
      pumpPreload();
    });
  }

  function buildSpare() {
    // With every video preloaded there is nothing left for a spare to do.
    // Only some of them (a windowed page), and it takes the row the mouse
    // is aiming at when that is outside the pool.
    if (ytSpare || !FETCH_AHEAD || (PRELOAD_ALL && !ytWindowed)) return;
    // Off-screen rather than display:none, same as the first one: see
    // render_player_page.
    var wrap = document.createElement('div');
    wrap.style.cssText =
      'position:fixed;left:-9999px;top:130px;width:200px;height:113px;';
    wrap.innerHTML = '<div></div>';
    document.body.appendChild(wrap);
    ytSpare = buildYt(wrap.firstChild, function () {
      ytSpareUp = true;
      preloadNext();
    });
  }

  window.onYouTubeIframeAPIReady = function () {
    ytApiReady = true;
    prewarm();
  };

  // Build the players while nothing is waiting on them, so the first press
  // pays for none of the setup. A press that lands before they are up is
  // queued in ensureYt and runs the moment the first one is.
  function prewarm() {
    ensureYt(null);
  }

  // ---- fetching ahead ----
  //
  // What makes a press slow is not the audio, it is everything YouTube does
  // before the audio: loadVideoById() on a standing player still took
  // 520-1060ms to first sound across five presses on a real digest page.
  // The same video already sitting in a second player starts in 190-390ms.
  // So the spare always holds the track most likely to be asked for next:
  // the one after what is playing (the first on the page before anything
  // is), or the row the mouse is aiming at, from the point on its bar the
  // pointer is over. A press on that track swaps the two players.
  //
  // cueVideoById() looks like the call for this and is not: it fetches the
  // thumbnail and nothing else, and playVideo() after it measured no faster
  // than loading cold (726-1150ms). It has to be a real, muted
  // loadVideoById(), held by onYtState as soon as it starts.

  // Seconds into the track for a point along the bar, from the length
  // Discogs gave us. 0 when that is unknown; the poll loop seeks then.
  function startAt(bar, fraction) {
    var known = knownDuration(bar);
    return fraction > 0.01 && known > 0 ? Math.floor(fraction * known) : 0;
  }

  function ytLoadArg(videoId, start) {
    return start > 0 ? { videoId: videoId, startSeconds: start } : videoId;
  }

  // Two points on a bar that are the same point to anyone listening: a
  // few seconds apart, or a handful of pixels. What the spare fetched from
  // one is played for a press on the other rather than fetched again.
  function closeEnough(bar, a, b) {
    return Math.abs(a - b) <= Math.max(3, knownDuration(bar) * 0.015);
  }

  // lastMoment: the press is already on its way (the button is down), so
  // there is no time left to fetch the same track again from another point.
  function preload(bar, fraction, lastMoment) {
    if (!ytSpareUp || !bar) return;
    var videoId = bar.getAttribute('data-yt');
    if (!videoId) return;  // direct MP3s start fast enough as they are
    if (activeEngine === 'yt' && videoId === activeVideoId) return;
    if (poolPlayerFor(videoId)) return;  // a pooled player has it already
    var start = startAt(bar, fraction);
    if (videoId === spareVideoId) {
      if (closeEnough(bar, start, spareStart)) return;
      // Seeking a player that has the video beat fetching it over
      // (530-860ms against 680ms and up), so the swap will seek instead.
      if (lastMoment && spareHeld) return;
    }
    spareBar = bar; spareVideoId = videoId; spareStart = start; spareHeld = false;
    spareAt = Date.now();
    try {
      ytSpare.mute();
      ytSpare.loadVideoById(ytLoadArg(videoId, start));
    } catch (err) { spareBar = null; spareVideoId = null; }
  }

  function preloadNext() {
    if (!ytSpareUp) return;
    // The pointer has only just settled on the track the spare is holding:
    // that one is the better guess for the next press.
    if (hoverBar && hoverBar === spareBar && Date.now() - hoverAt < 4000) return;
    if (activeBar && !activeBar.isConnected) return;
    preload(neighbour(activeBar, 1), 0);
  }

  // The swap asks a player that has only ever been muted and paused to
  // play out loud. If a browser will not have that, fall back to loading
  // the track the ordinary way rather than leave the press hanging.
  function watchSwap(videoId, start) {
    swapPending = true;
    clearTimeout(swapTimer);
    swapTimer = setTimeout(function () {
      if (!swapPending || activeEngine !== 'yt' || activeVideoId !== videoId) return;
      swapPending = false;
      var state = -1;
      try { state = ytPlayer.getPlayerState(); } catch (err) {}
      // Buffering is slow, not stuck: it was swapped in mid-fetch on a poor
      // connection, and starting that fetch over would only make it slower.
      if (state === 1 || state === 3) return;
      // Unstarted (-1) with this very video is YouTube holding it back
      // before it starts (see playYtFrom). Loading it again restarts that
      // wait, which the console log caught doing exactly that: 7.8s for one
      // that was retried twice. Only paused or cued -- the browser would not
      // let a muted player play out loud -- or a different video is stuck.
      var holding = ytVideoId(ytPlayer);
      if (state === -1 && (!holding || holding === videoId)) {
        logWarn('YouTube is holding this video back before it starts; waiting for it');
        return;
      }
      logWarn('not started after 1.5s (player state ' + state + '), loading it again');
      try { ytPlayer.loadVideoById(ytLoadArg(videoId, start)); } catch (err) {}
    }, 1500);
  }

  function playYtFrom(bar, fraction) {
    var videoId = bar.getAttribute('data-yt');
    logHow('waited for the YouTube player to start up');  // until the callback runs
    ensureYt(function () {
      if (activeBar && activeBar !== bar) resetBar(activeBar);
      if (activeEngine !== 'yt' || activeVideoId !== videoId) {
        var start = startAt(bar, fraction);
        // startSeconds puts the first request at the right place. Loading
        // from 0:00 and seeking afterwards, as this used to, fetches the
        // opening of the track only to throw it away.
        pendingSeekFraction = fraction > 0.01 && start === 0 ? fraction : null;
        swapPending = false; nearEndDone = false;
        // A pooled player still on its way is used too: it has a head start.
        // YouTube holds some videos back before it starts them -- mostly
        // official/label uploads, 6-20s of black at state -1, measured in a
        // bare player on this site in a signed-in Chrome with no ad blocker,
        // and the same in any player. Loading such a track again from cold,
        // as this briefly did, starts that wait over from zero (17-18s in
        // the console log, against a few for the ones that kept going).
        var pooled = poolPlayerFor(videoId);
        var swap = !pooled && ytSpareUp && spareVideoId === videoId;
        if (swap && !spareHeld && Date.now() - spareAt > 1000) {
          // Asked for over a second ago and not even buffering: that fetch
          // is not coming (blocked, most likely). Quicker to load it here.
          var spareState = -1;
          try { spareState = ytSpare.getPlayerState(); } catch (err) {}
          if (spareState !== 3) { swap = false; spareBar = null; spareVideoId = null; }
        }
        activeEngine = 'yt'; activeVideoId = videoId; activeSrc = null; activeBar = bar;
        if (pooled) {
          logHow((pooled.held ? 'preloaded' : 'preload still starting, ' +
            Math.round((Date.now() - pooled.startedAt) / 1000) + 's head start') +
            (pooled.at ? ', held at ' + fmt(pooled.at) : ''));
          playPooled(pooled, bar, fraction);
        } else if (swap) {
          logHow(spareHeld ? 'fetched ahead (hover/next)' : 'fetched ahead, still loading');
          // Muted on its way out: if it was itself still loading, it must
          // not be heard for the instant before onYtState holds it.
          try { ytPlayer.pauseVideo(); ytPlayer.mute(); } catch (err) {}
          // A pooled player stays in the pool, holding its track: the
          // main player is the one that becomes the next spare.
          var was = ytPlayer.__pool ? ytMain : ytPlayer;
          ytPlayer = ytSpare; ytSpare = was; ytMain = ytPlayer;
          spareBar = null; spareVideoId = null;
          ytPlayer.unMute();
          var seek = !closeEnough(bar, start, spareStart);
          if (seek) ytPlayer.seekTo(start, true);
          watchSwap(videoId, start);
          if (spareHeld) {
            ytPlayer.playVideo();
            // The player that just went quiet is free at once. Not waiting
            // for this track to sound first is what keeps a run of quick
            // "next" presses from outrunning the fetch every time.
            if (!seek) preloadNext();
          }
          // Otherwise it is still fetching, and it is now the player you
          // hear, so it starts by itself when it gets there. Both things
          // done above for a held one make that later, not sooner:
          // playVideo() into a load in progress, and a second player
          // beginning its own load beside it (the two share a thread),
          // each measured several hundred ms slower than leaving it be.
          // A seek is a fetch too, hence no preloadNext() beside one;
          // onYtState does it once this track is playing.
        } else {
          // Not preloaded yet. Never into a preloaded player that happens
          // to be the one you hear: that would throw away the video it
          // holds. Back to the main player instead.
          var pe = ytPool[videoId];
          logHow('cold load' + (pe ? ' (preload: ' + (pe.player ? pe.state : 'not near yet') + ')' : ''));
          if (PRELOAD_ALL && ytMain && ytPlayer !== ytMain) {
            silence(ytPlayer);
            ytPlayer = ytMain;
            try { ytPlayer.unMute(); } catch (err) {}
          }
          ytPlayer.loadVideoById(ytLoadArg(videoId, start));
        }
      } else {
        // A click on the bar of the track already playing (or paused).
        // Timed until it plays from the new point: onYtState when the seek
        // made it buffer, pollYt when it had that part already.
        logHow('jump within this track');
        activeBar = bar;
        var dur = ytPlayer.getDuration() || knownDuration(bar);
        if (dur > 0) {
          ytPlayer.seekTo(fraction * dur, true);
          if (pressLog) pressLog.seekTo = fraction * dur;
        }
        ytPlayer.playVideo();
      }
      stopPolling();
      pollTimer = setInterval(pollYt, 250);
      paint(bar, fraction, fmt(fraction * (ytPlayer.getDuration() || knownDuration(bar))),
            durLabel(bar), 'loading');
    });
  }

  function silence(player) {
    if (!player) return;
    try { player.pauseVideo(); player.mute(); } catch (err) {}
  }

  // Make a preloaded player the one you hear. It keeps its video when it
  // goes quiet again, so coming back to a track is as quick as the first
  // time.
  function playPooled(entry, bar, fraction) {
    if (ytPlayer !== entry.player) {
      silence(ytPlayer);
      ytPlayer = entry.player;
    }
    try { ytPlayer.unMute(); } catch (err) {}
    if (!entry.held) {
      // Still on its way. It is now the one you hear, so it starts by
      // itself when it gets there, and the poll loop seeks once it knows
      // the length -- same as a swap onto a spare that is still fetching.
      pendingSeekFraction = fraction > 0.01 ? fraction : null;
      watchSwap(entry.key, 0);
      return;
    }
    pendingSeekFraction = null;
    var dur = 0, cur = 0;
    try { dur = ytPlayer.getDuration() || 0; cur = ytPlayer.getCurrentTime() || 0; } catch (err) {}
    dur = dur || knownDuration(bar);
    var target = fraction > 0.01 && dur > 0 ? fraction * dur : 0;
    // Held a split second after 0:00 when it was preloaded, or wherever it
    // was left when last heard: only seek when that is noticeably off.
    if (Math.abs(cur - target) > 2) ytPlayer.seekTo(target, true);
    ytPlayer.playVideo();
    watchSwap(entry.key, Math.floor(target));
  }

  // ---- direct-audio backend (clone.nl, or any source with real MP3s) ----

  function pollAudio() {
    if (!activeBar) return;
    var dur = audioEl.duration || 0;
    var cur = audioEl.currentTime || 0;
    var effectiveDur = dur || knownDuration(activeBar);
    var frac = effectiveDur > 0 ? cur / effectiveDur : 0;
    var state = !audioEl.paused && !audioEl.ended ? 'playing'
               : (audioEl.readyState < 2 ? 'loading' : 'paused');
    paint(activeBar, frac, fmt(cur), dur > 0 ? fmt(dur) : durLabel(activeBar), state);
  }

  // Every <audio> gets these, preloaded ones included; each only acts while
  // its element is the one you hear.
  function wireAudio(el) {
    el.addEventListener('ended', function () {
      if (el === audioEl && activeEngine === 'audio' && activeBar) {
        var d = audioEl.duration || knownDuration(activeBar);
        paint(activeBar, 1, fmt(d), fmt(d), 'paused');
        stopPolling();
        step(1);  // roll on to the next track, top to bottom
      }
    });
    el.addEventListener('error', function () {
      if (el === audioEl && activeEngine === 'audio' && activeBar) {
        var r = row(activeBar);
        if (r) r.classList.add('errored');
        logWarn('MP3 could not be played (error ' + (el.error ? el.error.code : '?') + ')' +
                (activeBar === steppedTo ? ', skipping to the next' : ''));
        if (pressLog) pressLog.done = true;
        if (activeBar === steppedTo) step(1);  // same as a dead video
      }
    });
    // An MP3 row can be followed by a YouTube one; line that up as well.
    el.addEventListener('playing', function () {
      if (el === audioEl && activeEngine === 'audio') { logSound(el.currentTime); preloadNext(); }
    });
    // A jump within a clip that is already playing fires no 'playing'.
    el.addEventListener('seeked', function () {
      if (el === audioEl && !el.paused && pressLog && pressLog.seekTo !== undefined) {
        logSound(el.currentTime);
      }
    });
  }
  wireAudio(audioBase);

  function safePlay() {
    var p = audioEl.play();
    if (p && p.catch) p.catch(function () {});  // ignore benign AbortError on rapid re-clicks
  }

  function playAudioFrom(bar, fraction) {
    var src = bar.getAttribute('data-src');
    if (activeBar && activeBar !== bar) resetBar(activeBar);
    if (activeEngine !== 'audio' || activeSrc !== src) {
      activeEngine = 'audio'; activeSrc = src; activeVideoId = null; activeBar = bar;
      // A preloaded element has (some of) the file already: play that one
      // rather than start the same download over in audioBase.
      var pooled = audioPool[src] && audioPool[src].el;
      logHow(pooled ? 'MP3, preload ' + audioPool[src].state : 'MP3, not preloaded');
      var el = pooled || audioBase;
      if (audioEl !== el) { audioEl.pause(); audioEl = el; }
      if (!pooled) audioEl.src = src;
      if (pooled && audioEl.duration) {
        // Also rewinds a clip that was heard before and left halfway.
        audioEl.currentTime = fraction > 0.01 ? fraction * audioEl.duration : 0;
      } else if (fraction > 0.01) {
        var onMeta = function () {
          el.currentTime = fraction * (el.duration || 0);
          el.removeEventListener('loadedmetadata', onMeta);
        };
        el.addEventListener('loadedmetadata', onMeta);
      }
      safePlay();
    } else {
      logHow('jump within this MP3');
      activeBar = bar;
      if (audioEl.duration) {
        audioEl.currentTime = fraction * audioEl.duration;
        if (pressLog) pressLog.seekTo = audioEl.currentTime;
      }
      safePlay();
    }
    stopPolling();
    pollTimer = setInterval(pollAudio, 200);
    paint(bar, fraction, fmt(fraction * (audioEl.duration || knownDuration(bar))),
          durLabel(bar), 'loading');
  }

  // ---- preloading everything ----
  //
  // The spare above makes one press quick: the one most likely next. This
  // makes every press quick, by loading the whole page before it is asked
  // for. Each YouTube video gets a hidden, muted player of its own, which
  // starts the video and is held right there (the same "held" state the
  // spare reaches -- 190-390ms to sound instead of 520-1060ms); each MP3
  // gets an <audio> of its own that downloads it. Both go top to bottom, a
  // few at a time, and the counter at the top of the page shows how far.
  //
  // Every YouTube player is a whole embedded page, so they cannot all be
  // standing at once: 2026-10-29 had 3008 videos on one page, one player
  // each, and that brought the browser to its knees. There are at most
  // POOL_MAX players, and they go where a press is most likely to land
  // next -- the tracks after the one playing, then the rows on screen, then
  // further ahead. When that moves (a press, a scroll), a player holding a
  // track that is no longer near gets the new one: loading a video into a
  // standing player is cheap, building the player is what is slow. On a
  // day with no more videos than that, this is simply all of them, as
  // before. Further than that, the spare takes the row the mouse is aiming
  // at, so a press far down the page is still fetched ahead.
  //
  // ?pool=N sets the number of players by hand; ?ahead=0 or the browser's
  // data saver turn all of it off, back to one track ahead.
  var POOL_PARALLEL = 3;      // YouTube players being set up at once
  var AUDIO_PARALLEL = 3;     // MP3s downloading at once
  // Stop holding a slot for one after this; it keeps loading and still
  // counts when it gets there. Short, because the slow ones are YouTube
  // holding a video back (up to ~20s), which costs no bandwidth: waiting
  // them out three at a time stalled the window behind them.
  var POOL_TIMEOUT = 5000;
  var PRESS_QUIET = 2000;     // after a press, start nothing new for this long
  var poolArg = /[?&]pool=(\d+)/.exec(location.search);
  // Measured on that page in Chrome: each standing player is ~40MB.
  // Unbounded, Chrome grew by ~260MB a minute for as long as the tab was
  // open (3.2GB at five minutes and climbing); with 24 it stays flat at
  // ~1.9GB, against ~0.9GB for the page with no preloading at all.
  var POOL_MAX = poolArg ? Math.max(1, parseInt(poolArg[1], 10)) : 24;
  // Skimming a track every 5-10s (the console log of 2026-10-29) outran a
  // dozen; some videos need up to 20s before YouTube lets them start.
  var POOL_AHEAD = 18;        // tracks after the one playing, always first in line
  var POOL_VIEW = 5;          // then this many from the top of the screen down
  var ytPool = {}, audioPool = {};   // video id / MP3 url -> entry
  var ytPlayers = [];         // every pooled player built so far (<= POOL_MAX)
  var ytTargets = [];         // video ids the pool should hold right now, best first
  var audioQueue = [], audioEntries = [];
  var ytBusy = 0, audioBusy = 0, ytPoolStarted = false, ytRefused = false;
  var ytWindowed = false;     // more videos on the page than POOL_MAX
  var everShown = !document.hidden, hiddenRefused = false;

  // ---- where you start a track ----
  //
  // The console log of 2026-10-29 had nearly every press somewhere along
  // the bar, 1:00-2:30 in, hardly any at 0:00. A player held at 0:00 then
  // has to fetch that part first: most of the 300-1200ms those presses
  // took. So the pool holds each video where you tend to start instead --
  // a little before it, because YouTube buffers ahead of where a player is
  // paused, not behind: a press anywhere in the next stretch plays from
  // what it already has. Learned from your last PRESS_HISTORY presses,
  // kept in this browser; mostly "next" (from 0:00) keeps it at 0:00.
  var PRESS_HISTORY = 20;
  var PRESS_KEY = 'digest.pressFractions';
  var pressFractions = [];
  try { pressFractions = JSON.parse(localStorage.getItem(PRESS_KEY) || '[]') || []; } catch (err) {}

  function rememberPress(fraction) {
    pressFractions.push(Math.round(fraction * 1000) / 1000);
    if (pressFractions.length > PRESS_HISTORY) pressFractions.shift();
    try { localStorage.setItem(PRESS_KEY, JSON.stringify(pressFractions)); } catch (err) {}
  }

  // The fraction of a track to hold preloaded videos at: the middle of
  // where you started, when at least 60% of recent presses were along the
  // bar; 0 otherwise. The middle rather than lower down, because a paused
  // player only buffers 10-22s ahead (measured on that page): held further
  // back, most presses landed past what it had.
  function holdFraction() {
    var along = pressFractions.filter(function (f) { return f > 0.05; });
    if (along.length < 3 || along.length < pressFractions.length * 0.6) return 0;
    along.sort(function (a, b) { return a - b; });
    return along[Math.floor((along.length - 1) / 2)];
  }

  // Seconds into this video to hold it at; 0 when its length is unknown.
  // A few seconds early, so a press just short of the usual point is
  // still inside what it buffered.
  function holdAt(entry) {
    var f = holdFraction();
    return f > 0 && entry.dur > 0 ? Math.max(0, Math.floor(f * entry.dur) - 3) : 0;
  }
  var allBars = [], recFirstBar = [], visibleRecs = {};
  var lastPressAt = 0, pumpTimer = null, scrollTimer = null, preloadLine = null;

  function markErrored(attr, value) {
    var bars = document.querySelectorAll('.bar[' + attr + ']');
    for (var i = 0; i < bars.length; i++) {
      if (bars[i].getAttribute(attr) !== value) continue;
      var r = row(bars[i]);
      if (r) r.classList.add('errored');
    }
  }

  // Each video / clip once however many rows share it. MP3s are cheap (an
  // <audio> each) and all go in line, top to bottom; YouTube videos only
  // get an entry here, and a player once they are near (see ytWanted).
  function collectPreload() {
    allBars = Array.prototype.slice.call(document.querySelectorAll('.bar'));
    var videos = 0;
    for (var i = 0; i < allBars.length; i++) {
      var yt = allBars[i].getAttribute('data-yt'), src = allBars[i].getAttribute('data-src');
      if (yt && !ytPool[yt]) {
        ytPool[yt] = { kind: 'yt', key: yt, state: 'queued', dur: knownDuration(allBars[i]) };
        videos++;
      } else if (!yt && src && !audioPool[src]) {
        audioPool[src] = { kind: 'audio', key: src, state: 'queued' };
        audioQueue.push(audioPool[src]);
        audioEntries.push(audioPool[src]);
      }
    }
    ytWindowed = videos > POOL_MAX;
    console.info('[digest] ' + videos + ' YouTube videos, ' + audioEntries.length + ' MP3s; ' +
      (ytWindowed ? 'preloading the ' + POOL_MAX + ' videos nearest to you' : 'preloading all of them'));
    watchScreen();
  }

  // Which records are on screen, so the pool can follow a scroll. Records
  // rather than bars: far fewer to watch, and a record that is off screen
  // is not laid out at all (content-visibility in PLAYER_CSS).
  function watchScreen() {
    if (!ytWindowed || !window.IntersectionObserver) return;
    var recs = document.querySelectorAll('.rec');
    var recIndex = new Map();
    for (var i = 0; i < recs.length; i++) recIndex.set(recs[i], i);
    for (var b = 0; b < allBars.length; b++) {
      var rec = allBars[b].closest('.rec');
      var ri = rec ? recIndex.get(rec) : undefined;
      if (ri !== undefined && recFirstBar[ri] === undefined) recFirstBar[ri] = b;
    }
    var io = new IntersectionObserver(function (changes) {
      for (var c = 0; c < changes.length; c++) {
        var idx = recIndex.get(changes[c].target);
        if (changes[c].isIntersecting) visibleRecs[idx] = true; else delete visibleRecs[idx];
      }
      // Settle first: a flick down the page passes hundreds of rows that
      // nobody is going to press.
      clearTimeout(scrollTimer);
      scrollTimer = setTimeout(pumpPreload, 400);
    });
    for (var r = 0; r < recs.length; r++) io.observe(recs[r]);
  }

  // The first bar on screen, or 0 when that is not known.
  function firstBarOnScreen() {
    var top = -1;
    for (var k in visibleRecs) {
      var n = +k;
      if (top === -1 || n < top) top = n;
    }
    if (top === -1) return 0;
    for (var ri = top; ri < recFirstBar.length; ri++) {
      if (recFirstBar[ri] !== undefined) return recFirstBar[ri];
    }
    return 0;
  }

  // The videos the pool should hold, best first: the one playing and the
  // ones after it, the one before it (a "previous" press), the rows on
  // screen, then on down the page from whichever of those comes first.
  function ytWanted() {
    var out = [], have = {};
    function take(from, dir, count) {
      for (var i = from; count > 0 && out.length < POOL_MAX && i >= 0 && i < allBars.length; i += dir) {
        var yt = allBars[i].getAttribute('data-yt');
        if (!yt || have[yt]) continue;
        var r = row(allBars[i]);
        if (r && r.classList.contains('errored')) continue;
        have[yt] = true; out.push(yt); count--;
      }
    }
    var active = activeBar ? allBars.indexOf(activeBar) : -1;
    var screen = ytWindowed ? firstBarOnScreen() : 0;
    if (active !== -1) {
      take(active, 1, POOL_AHEAD);
      take(active - 1, -1, 1);
    }
    take(screen, 1, POOL_VIEW);
    take(active !== -1 ? Math.min(active, screen) : screen, 1, POOL_MAX);
    return out;
  }

  function pumpPreload() {
    clearTimeout(pumpTimer); pumpTimer = null;
    // A tab opened in the background may not start video, so wait for it
    // to be shown once. After that, carry on while it is in the background:
    // listening top to bottom in another tab is exactly when the pool must
    // keep moving, or every track after the first dozen loads cold.
    if (!PRELOAD_ALL || (document.hidden && (!everShown || hiddenRefused))) return;
    // A press gets the connection to itself: a load starting beside it was
    // measured to make it several hundred ms slower (see the swap above).
    var quiet = PRESS_QUIET - (Date.now() - lastPressAt);
    if (quiet > 0) { pumpTimer = setTimeout(pumpPreload, quiet); return; }
    ytTargets = ytWanted();
    if (ytPoolStarted && !ytRefused) {
      var wanted = {};
      for (var w = 0; w < ytTargets.length; w++) wanted[ytTargets[w]] = true;
      for (var t = 0; t < ytTargets.length && ytBusy < POOL_PARALLEL; t++) {
        var entry = ytPool[ytTargets[t]];
        if (entry.state !== 'queued' || entry.busy) continue;
        if (!entry.player && ytPlayers.length >= POOL_MAX && !reclaimPlayer(entry, wanted)) break;
        startYtPreload(entry);
      }
    }
    while (audioBusy < AUDIO_PARALLEL && audioQueue.length) startAudioPreload(audioQueue.shift());
    paintPreload();
  }

  // Hand a player that holds a track nobody is near any more to `entry`.
  // Never the one you hear, and never one still setting up.
  function reclaimPlayer(entry, wanted) {
    for (var i = 0; i < ytPlayers.length; i++) {
      var p = ytPlayers[i], old = p.__pool;
      if (p === ytPlayer || !old || old.busy || !old.up || wanted[old.key]) continue;
      silence(p);
      old.player = null; old.up = false; old.held = false; old.state = 'queued';
      entry.player = p; entry.up = true; entry.held = false;
      p.__pool = entry;
      return true;
    }
    return false;
  }

  function startYtPreload(entry) {
    ytBusy++; entry.busy = true; entry.state = 'loading'; entry.startedAt = Date.now();
    entry.at = holdAt(entry);  // decided now, so playPooled knows where it is held
    entry.timer = setTimeout(function () { poolRelease(entry); }, POOL_TIMEOUT);
    if (entry.player) {
      // A standing player: back from being blocked in a hidden tab, or
      // handed over by reclaimPlayer.
      if (entry.up) {
        try {
          entry.player.mute();
          entry.player.loadVideoById(ytLoadArg(entry.key, entry.at));
        } catch (err) { poolSettle(entry, 'failed'); }
      }
      return;
    }
    var wrap = document.createElement('div');
    wrap.style.cssText = 'position:fixed;left:-9999px;top:260px;width:200px;height:113px;';
    wrap.innerHTML = '<div></div>';
    document.body.appendChild(wrap);
    var player = buildYt(wrap.firstChild, function () {
      // By whichever entry holds it now: it may have changed hands while
      // it was being built.
      var owner = player.__pool;
      if (!owner) return;
      owner.up = true;
      try {
        player.mute();
        player.loadVideoById(ytLoadArg(owner.key, owner.at));
      } catch (err) { poolSettle(owner, 'failed'); }
    });
    entry.player = player;
    player.__pool = entry;
    ytPlayers.push(player);
  }

  function startAudioPreload(entry) {
    audioBusy++; entry.busy = true; entry.state = 'loading';
    entry.timer = setTimeout(function () { poolRelease(entry); }, POOL_TIMEOUT);
    var el = new Audio();
    el.preload = 'auto';
    wireAudio(el);
    // canplaythrough: the browser reckons it can play to the end without
    // stopping to buffer, which is the whole point.
    el.addEventListener('canplaythrough', function () { poolSettle(entry, 'loaded'); });
    el.addEventListener('error', function () {
      // 4 is what a missing file (404) turns into; a network blip is not
      // a reason to grey the row out.
      if (el.error && el.error.code === 4) markErrored('data-src', entry.key);
      poolSettle(entry, 'failed');
    });
    entry.el = el;
    el.src = entry.key;
  }

  // Frees the slot, without saying how it went: one that timed out keeps
  // loading, and still counts if it gets there.
  function poolRelease(entry) {
    if (!entry.busy) return;
    entry.busy = false;
    clearTimeout(entry.timer);
    if (entry.kind === 'yt') ytBusy--; else audioBusy--;
    pumpPreload();
  }

  function poolSettle(entry, state) {
    if (entry.state !== 'loaded') entry.state = state;
    poolRelease(entry);
    paintPreload();
  }

  // A tab opened in the background may not start anything, muted or not.
  // Those go back in line, first, for when the tab is shown.
  function poolBlocked(entry) {
    if (entry.state === 'loaded') return;
    if (!document.hidden) {
      // Shown, and still refused: this browser lets no page start video by
      // itself, muted or not. Building the rest would only cost memory.
      ytRefused = true;
      poolSettle(entry, 'failed');
      return;
    }
    // Back in line, and nothing more until the tab is shown again.
    if (!hiddenRefused) console.info('[digest] browser paused preloading in this background tab');
    hiddenRefused = true;
    entry.state = 'queued';
    poolRelease(entry);
  }

  // Only a player whose video is loaded or loading: one whose embed is
  // still being built would be slower than loading into the main player.
  function poolPlayerFor(videoId) {
    var entry = ytPool[videoId];
    return entry && entry.player && entry.up && entry.state !== 'failed' &&
      entry.state !== 'queued' ? entry : null;
  }

  // Counted over what is wanted right now: on a windowed page that is the
  // tracks near you, so the pill fills up again after a jump down the page.
  function paintPreload() {
    if (!preloadEl) return;
    var entries = audioEntries.slice();
    for (var t = 0; t < ytTargets.length; t++) entries.push(ytPool[ytTargets[t]]);
    var total = entries.length, loaded = 0, failed = 0, waiting = 0;
    for (var i = 0; i < total; i++) {
      var st = entries[i].state;
      if (st === 'loaded') loaded++;
      else if (st === 'failed') failed++;
      else if (st === 'queued' && !(entries[i].kind === 'yt' && ytRefused)) waiting++;
    }
    if (!total) return;
    var pct = Math.floor(loaded * 100 / total);
    var finished = !waiting && !ytBusy && !audioBusy;
    var label = ytWindowed
      ? (finished ? 'Ready ' : 'Preloading ') + loaded + '/' + total + ' nearby'
      : (finished ? 'Preloaded ' : 'Preloading ') + pct + '%' +
        (pct < 100 ? ' \u00b7 ' + loaded + '/' + total : '');
    preloadEl.hidden = false;
    preloadEl.classList.toggle('done', finished);
    preloadEl.querySelector('.plabel').textContent = label;
    preloadEl.querySelector('.pfill').style.width = pct + '%';
    preloadEl.title = loaded + ' of ' + total + ' tracks ready to play' +
      (ytWindowed ? ' (the ones after what is playing and on screen; ' +
        'this moves along as you listen and scroll)' : '') +
      (failed ? ', ' + failed + ' could not be loaded' : '') +
      (ytRefused ? '. This browser does not allow preloading video.' : '');
    // A thin line along the top of the window, so the progress stays in
    // sight further down the page. Gone once there is nothing left to do.
    if (!preloadLine) {
      preloadLine = document.createElement('div');
      preloadLine.id = 'preloadline';
      document.body.appendChild(preloadLine);
    }
    preloadLine.style.width = pct + '%';
    preloadLine.hidden = finished;
  }

  // ---- track navigation: walk the page top to bottom ----

  // The first playable bar after (delta 1) or before (-1) `bar`, counted
  // from where `bar` sits on the page even when it has just been greyed
  // out itself. Looking it up among the playable ones only, as this used
  // to, lost a dead track's place and "next" went back to the top.
  // null at the ends: stop there, don't wrap. Read from the page on each
  // call, so a row marked .errored mid-session (a dead YouTube video or a
  // 404 preview) is skipped from then on.
  function neighbour(bar, delta) {
    var all = document.querySelectorAll('.bar');
    var idx = bar ? Array.prototype.indexOf.call(all, bar) : -1;
    var i = idx === -1 ? (delta > 0 ? 0 : all.length - 1) : idx + delta;
    for (; i >= 0 && i < all.length; i += delta) {
      var r = row(all[i]);
      if (!r || !r.classList.contains('errored')) return all[i];
    }
    return null;
  }

  function step(delta) {
    var target = neighbour(activeBar, delta);
    if (!target) return;
    playFrom(target, 0, true);
    if (target.scrollIntoView) {
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
    try { target.focus({ preventScroll: true }); } catch (err) { target.focus(); }
  }

  // ---- floating transport ----

  // The generated digest pages ship this markup in their HTML; likes.html
  // does not, and building it here rather than hand-copying it there keeps
  // the transport defined in exactly one place. Both call sites end up with
  // the same controls, and adding a third page costs nothing.
  function ensureChrome() {
    if (!document.body) return;
    if (!document.getElementById('yt-audio-host')) {
      var host = document.createElement('div');
      host.style.cssText =
        'position:fixed;left:-9999px;top:0;width:200px;height:113px;';
      host.innerHTML = '<div id="yt-audio-host"></div>';
      document.body.appendChild(host);
    }
    if (!document.getElementById('nowbar')) {
      var nb = document.createElement('div');
      nb.id = 'nowbar';
      nb.innerHTML =
        '<div class="nbseek" role="slider" aria-valuemin="0" aria-valuemax="100" ' +
        'aria-valuenow="0" aria-label="Seek in current track">' +
        '<div class="nbtrack"><div class="nbfill"></div></div></div>' +
        '<div class="nbrow"><div class="nbctrls">' +
        '<button type="button" data-step="-1" title="Previous track (p)" ' +
        'aria-label="Previous track">' + ICON_PREV + '</button>' +
        '<button type="button" data-toggle="1" title="Play / pause (space)" ' +
        'aria-label="Play or pause">' + ICON_PAUSE + '</button>' +
        '<button type="button" data-step="1" title="Next track (n)" ' +
        'aria-label="Next track">' + ICON_NEXT + '</button>' +
        '</div><span class="nowtitle"></span>' +
        '<span class="nbtime"></span></div>';
      document.body.appendChild(nb);
    }
  }

  ensureChrome();
  var nowbar = document.getElementById('nowbar');
  var nowtitle = nowbar ? nowbar.querySelector('.nowtitle') : null;

  function updateNowBar(bar) {
    if (!nowbar) return;
    if (!bar) { nowbar.classList.remove('show'); return; }
    nowbar.classList.add('show');
    if (nowtitle) {
      var li = bar.closest ? bar.closest('li.track') : null;
      var name = li ? li.querySelector('.tname') : null;
      nowtitle.textContent = name ? name.textContent : (bar.getAttribute('aria-label') || '');
    }
  }

  if (nowbar) {
    nowbar.addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('button') : null;
      if (btn) {
        ev.preventDefault();
        if (btn.getAttribute('data-toggle')) {
          if (activeBar) togglePlayPause(activeBar);
        } else {
          step(parseInt(btn.getAttribute('data-step'), 10) || 1);
        }
        return;
      }
      // Seeking from the bottom bar acts on whatever is currently playing,
      // so you can scrub without scrolling back to that record's row.
      var seek = ev.target.closest ? ev.target.closest('.nbseek') : null;
      if (seek && activeBar) {
        ev.preventDefault();
        playFrom(activeBar, fractionFromEvent(seek, ev));
      }
    });
  }

  // ---- unified dispatch: only one backend plays at a time ----

  // viaStep: reached by next/previous or auto-advance rather than picked
  // by hand. A track like that which turns out to be dead is skipped; one
  // you picked yourself just greys out.
  function playFrom(bar, fraction, viaStep) {
    lastPressAt = Date.now();
    // Where a track was started, not a jump within the one playing.
    if (bar !== activeBar) rememberPress(fraction);
    steppedTo = viaStep ? bar : null;
    var li = bar.closest ? bar.closest('li.track') : null;
    var tn = li ? li.querySelector('.tname') : null;
    pressLog = { t: performance.now(), how: '?', fromBar: fraction > 0.01,
                 name: tn ? tn.textContent : (bar.getAttribute('aria-label') || '') };
    var isAudio = !!bar.getAttribute('data-src');
    if (isAudio) {
      if (activeEngine === 'yt' && ytPlayer) { try { ytPlayer.pauseVideo(); } catch (err) {} }
      playAudioFrom(bar, fraction);
    } else {
      if (activeEngine === 'audio') { audioEl.pause(); }
      playYtFrom(bar, fraction);
    }
    updateNowBar(bar);
    // Moves the pool along to the tracks after this one, once the press
    // has had the connection to itself.
    pumpPreload();
  }

  function togglePlayPause(bar) {
    var isAudio = !!bar.getAttribute('data-src');
    var isActiveTrack = isAudio
      ? (activeEngine === 'audio' && activeSrc === bar.getAttribute('data-src'))
      : (activeEngine === 'yt' && activeVideoId === bar.getAttribute('data-yt'));
    if (!isActiveTrack) { playFrom(bar, 0); return; }
    swapPending = false;  // a pause this soon after a swap is the listener's, not a failure
    if (isAudio) {
      if (audioEl.paused) { safePlay(); } else { audioEl.pause(); }
    } else if (ytPlayer) {
      var state = ytPlayer.getPlayerState();
      if (state === 1) { ytPlayer.pauseVideo(); } else { ytPlayer.playVideo(); }
    }
  }

  function fractionFromEvent(bar, ev) {
    var rect = bar.getBoundingClientRect();
    var x = (ev.clientX !== undefined && ev.clientX !== 0) ? ev.clientX
          : (ev.changedTouches && ev.changedTouches[0] ? ev.changedTouches[0].clientX : rect.left);
    return Math.min(1, Math.max(0, (x - rect.left) / rect.width));
  }

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('button.ppbtn') : null;
    if (btn) {
      ev.preventDefault();
      var pbar = row(btn).querySelector('.bar');
      if (pbar) togglePlayPause(pbar);
      return;
    }
    if (ev.target.closest && ev.target.closest('a.ytlink')) return; // let it navigate
    var bar = ev.target.closest ? ev.target.closest('.bar') : null;
    if (bar) {
      ev.preventDefault();
      playFrom(bar, fractionFromEvent(bar, ev));
    }
  });

  // Start fetching before the press lands. A mouse comes to rest on a
  // control a few hundred ms before its button goes down, and the button
  // is down for another ~100ms before the click. Neither moment plays
  // anything -- that still takes the click.
  //
  // Mouse only. A finger gives no hover, and its pointerdown is also how
  // every scroll starts: fetching on that would spend mobile data on rows
  // that were only being flicked past.

  // The bar a play button or a bar belongs to; null anywhere else.
  function barUnder(target) {
    var el = target && target.closest ? target.closest('button.ppbtn, .bar') : null;
    var r = el ? row(el) : null;
    return r ? r.querySelector('.bar') : null;
  }

  function preloadAt(bar, target, x, lastMoment) {
    var onBar = target.closest('.bar');
    preload(bar, onBar ? fractionFromEvent(bar, { clientX: x }) : 0, lastMoment);
  }

  document.addEventListener('pointerdown', function (ev) {
    if (ev.pointerType !== 'mouse' || ev.button) return;
    var bar = barUnder(ev.target);
    if (!bar) return;
    clearTimeout(hoverTimer);
    preloadAt(bar, ev.target, ev.clientX, true);
  });

  document.addEventListener('pointermove', function (ev) {
    if (ev.pointerType !== 'mouse') return;
    clearTimeout(hoverTimer);
    var bar = barUnder(ev.target), target = ev.target, x = ev.clientX, now = Date.now();
    if (bar !== hoverBar) { hoverBar = bar; hoverEnterAt = now; }
    hoverAt = now;
    if (!bar) {
      // Off the controls again: hand the spare back to whatever plays
      // next, in case a row the pointer paused on borrowed it.
      hoverTimer = setTimeout(preloadNext, 1500);
      return;
    }
    // A row the spare does not have yet: fetch once the pointer has been
    // on its control for 100ms, still moving or not. That is long enough
    // to tell aiming at it from passing over it, and every ms waited
    // beyond that comes straight off the head start. Never within half a
    // second of the last one, though, so a slow sweep down the list cannot
    // turn into a burst of requests. A row it does have, from a different
    // point on the bar: only once the pointer has stopped there.
    var wait = spareBar === bar ? 150
      : Math.max(100 - (now - hoverEnterAt), 500 - (now - hoverFetchAt), 0);
    hoverTimer = setTimeout(function () {
      if (spareBar !== bar) hoverFetchAt = Date.now();
      preloadAt(bar, target, x, false);
    }, wait);
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    var t = ev.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;

    // Next / previous work from anywhere on the page, so you can skip
    // through without first clicking a row to focus it. Deliberately NOT
    // bound to bare Up/Down arrows at this level -- those still need to
    // scroll the page normally.
    var k = ev.key.toLowerCase();
    if (k === 'n' || k === 'j' || ev.key === 'MediaTrackNext') {
      ev.preventDefault(); step(1); return;
    }
    if (k === 'p' || k === 'k' || ev.key === 'MediaTrackPrevious') {
      ev.preventDefault(); step(-1); return;
    }

    var el = document.activeElement;
    if (!el || !el.classList || !el.classList.contains('bar')) return;
    if (ev.key === ' ' || ev.key === 'Enter') {
      ev.preventDefault();
      togglePlayPause(el);
    } else if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      // Only once a bar has focus, where arrow keys clearly belong to the
      // player rather than to scrolling the page.
      ev.preventDefault();
      step(ev.key === 'ArrowDown' ? 1 : -1);
    } else if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
      ev.preventDefault();
      if (activeBar !== el) return;
      var delta = ev.key === 'ArrowRight' ? 5 : -5;
      if (activeEngine === 'audio') {
        audioEl.currentTime = Math.min(audioEl.duration || 1e9, Math.max(0, audioEl.currentTime + delta));
      } else if (activeEngine === 'yt' && ytPlayer) {
        var dur = ytPlayer.getDuration() || knownDuration(el);
        var cur = ytPlayer.getCurrentTime() || 0;
        ytPlayer.seekTo(Math.min(dur, Math.max(0, cur + delta)), true);
      }
    }
  });

  // Exported so likes.html can build identical track rows from the data
  // stored in likes.json. One definition of the markup, several pages --
  // a second hand-written copy over there would drift the moment either
  // the structure or the icons change. The click/keyboard handlers above
  // are delegated on document, so rows added later still work.
  window.DigestPlayer = {
    trackRowHtml: function (t) {
      var esc = function (s) {
        return String(s == null ? '' : s)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      };
      var title = String(t.title || 'Listen');
      var shown = title.length > 70 ? title.slice(0, 69).replace(/\s+$/, '') + '\u2026' : title;
      var dur = parseInt(t.dur, 10) || 0;
      var ytAttr = t.yt ? ' data-yt="' + esc(t.yt) + '"' : '';
      var srcAttr = t.src ? ' data-src="' + esc(t.src) + '"' : '';
      var link = t.yt
        ? '<a class="ytlink" href="https://www.youtube.com/watch?v=' + esc(t.yt) +
          '" target="_blank" rel="noopener noreferrer" title="Watch on YouTube" ' +
          'aria-label="Watch on YouTube">\u2197</a>'
        : '';
      return '<li class="track"><div class="trow">' +
        '<button class="ppbtn" aria-label="Play ' + esc(shown) + '">' + ICON_PLAY + '</button>' +
        '<div class="bar" tabindex="0" role="slider" aria-valuemin="0" ' +
        'aria-valuemax="100" aria-valuenow="0" aria-label="' + esc(title) + '"' +
        ytAttr + srcAttr + ' data-dur="' + dur + '">' +
        '<div class="track"><div class="fill"></div></div></div>' +
        '<span class="time">0:00 / ' + (dur ? fmt(dur) : '--:--') + '</span>' +
        link + '</div><div class="tname">' + esc(shown) + '</div></li>';
    }
  };

  // The rows are all in the page by now (this script sits at the end of
  // it). MP3s can start straight away; YouTube waits for the main player.
  if (PRELOAD_ALL) {
    collectPreload();
    paintPreload();  // 0% from the start, even in a tab that waits until shown
    pumpPreload();
  }

  // Three cases, in order: the API is already loaded (its ready callback
  // fired before this script existed, so nothing will call ours -- warm up
  // directly); the page's <head> has the request in flight (leave it, our
  // onYouTubeIframeAPIReady above will fire); or nobody asked for it yet.
  if (window.YT && window.YT.Player) {
    ytApiReady = true;
    prewarm();
  } else if (!document.querySelector('script[src*="youtube.com/iframe_api"]')) {
    var tag = document.createElement('script');
    tag.src = 'https://www.youtube.com/iframe_api';
    document.head.appendChild(tag);
  }
})();
