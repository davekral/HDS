/* ----------------------------------------------------------------------- *
 *  Hlasový kuchyňský asistent – frontend
 *
 *  Stará se o:
 *    - připojení ke SpeechCloud (poslouchání + mluvení) a k našemu
 *      dialogovému manažeru (lokální WebSocket /ws),
 *    - vykreslení chatu, panelu s receptem a hlasového režimu,
 *    - obousměrnou komunikaci s DM přes dm_send_message / dm_receive_message.
 * ----------------------------------------------------------------------- */

// Adresa SpeechCloud modelu (ASR + TTS). Shodná s příklady v repozitáři.
var SPEECHCLOUD_URI = "https://speechcloud.kky.zcu.cz:9443/v1/speechcloud/edu-hds-all";

$(document).ready(function () {

  /* === SpeechCloud ===================================================== */
  var speechCloud = new SpeechCloud({
    uri: SPEECHCLOUD_URI,
    tts: "#audioout",   // sem se přehrává syntetizovaná řeč
    local_dm: "/ws",    // náš dialogový manažer (Tornado)
  });
  window.speechCloud = speechCloud;

  var state = {
    recipeLoaded: false,
    voiceMode: false,
    ready: false,
    typingEl: null,
  };

  /* --- Pomůcka pro odeslání dat do DM --------------------------------- */
  function sendToDM(data) {
    speechCloud.dm_send_message({ data: data });
  }

  /* === Vykreslování chatu ============================================= */
  var $messages = $("#messages");

  function addMessage(role, text) {
    removeTyping();
    var avatar = role === "user" ? "🧑‍🍳" : (role === "assistant" ? "🍳" : "");
    var $msg = $('<div class="msg ' + role + '"></div>');
    if (avatar) $msg.append('<div class="msg-avatar">' + avatar + "</div>");
    $msg.append($('<div class="msg-bubble"></div>').text(text));
    $messages.append($msg);
    scrollChat();
    if ((role === "assistant" || role === "user") && state.voiceMode) {
      addVoiceLog(role, text);
    }
    return $msg;
  }

  function showTyping() {
    removeTyping();
    state.typingEl = $(
      '<div class="msg assistant"><div class="msg-avatar">🍳</div>' +
      '<div class="msg-bubble"><span class="typing"><span></span><span></span><span></span></span></div></div>'
    );
    $messages.append(state.typingEl);
    scrollChat();
  }
  function removeTyping() {
    if (state.typingEl) { state.typingEl.remove(); state.typingEl = null; }
  }
  function scrollChat() { $messages.scrollTop($messages[0].scrollHeight); }

  /* === Stavový indikátor ============================================== */
  var STATUS_TEXT = {
    idle: "", thinking: "Přemýšlím…", speaking: "Mluvím…", listening: "Poslouchám…",
  };
  function setStatus(s) {
    var label = STATUS_TEXT[s] || "";
    var $chip = $("#status-chip");
    if (label && s !== "idle") { $chip.text(label).prop("hidden", false); }
    else { $chip.prop("hidden", true); }

    if (s === "thinking") showTyping(); else removeTyping();

    // Hlasový režim – orb + text
    $("#voice-orb").removeClass("idle listening thinking speaking").addClass(s);
    var vtext = { idle: "Připraven", thinking: "Přemýšlím…",
                  speaking: "Mluvím…", listening: "Poslouchám… mluvte" };
    $("#voice-state").text(vtext[s] || "");
  }

  /* === Panel s receptem =============================================== */
  function renderRecipe(r) {
    state.recipeLoaded = true;
    $("#recipe-panel").removeClass("empty").addClass("show");
    $(".recipe-content").prop("hidden", false);
    $("#recipe-title").text(r.title || "Recept");

    var $ing = $("#ingredient-list").empty();
    (r.ingredients || []).forEach(function (it) { $ing.append($("<li></li>").text(it)); });

    var $steps = $("#step-list").empty();
    (r.steps || []).forEach(function (st) { $steps.append($("<li></li>").text(st)); });

    updateProgress({ index: -1, total: (r.steps || []).length, started: false, finished: false });
  }

  function updateProgress(p) {
    var total = p.total || 0;
    var shown = p.started ? (p.index + 1) : 0;
    var pct = total ? (shown / total) * 100 : 0;
    $("#progress-fill").css("width", pct + "%");
    $("#progress-label").text("krok " + shown + " / " + total);

    $("#step-list li").each(function (i) {
      $(this).removeClass("active done");
      if (!p.started) return;
      if (i < p.index) $(this).addClass("done");
      else if (i === p.index) {
        $(this).addClass("active");
        if (this.scrollIntoView) this.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    });
  }

  /* === Hlasový režim (overlay) ======================================== */
  function addVoiceLog(role, text) {
    var $log = $("#voice-log");
    $log.append($('<div class="vmsg ' + role + '"></div>').text(text));
    $log.scrollTop($log[0].scrollHeight);
  }

  function openVoiceMode() {
    if (!state.ready) { addMessage("system", "Počkejte prosím na připojení."); return; }
    state.voiceMode = true;
    $("#voice-overlay").prop("hidden", false);
    $("#btn-voice").addClass("active");
    $("#voice-toggle").text("Pozastavit poslech").data("paused", false);
    sendToDM({ type: "set_voice_mode", enabled: true });
  }
  function closeVoiceMode() {
    state.voiceMode = false;
    $("#voice-overlay").prop("hidden", true);
    $("#btn-voice").removeClass("active");
    $("#voice-caption").text("");
    sendToDM({ type: "set_voice_mode", enabled: false });
    setStatus("idle");
  }

  $("#btn-voice").click(function () { state.voiceMode ? closeVoiceMode() : openVoiceMode(); });
  $("#voice-close").click(closeVoiceMode);
  $("#voice-toggle").click(function () {
    var paused = !$(this).data("paused");
    $(this).data("paused", paused).text(paused ? "Pokračovat v poslechu" : "Pozastavit poslech");
    sendToDM({ type: "set_voice_mode", enabled: !paused });
    if (paused) setStatus("idle");
  });

  /* === Odesílání textu ================================================ */
  var $input = $("#input");
  function sendText() {
    var text = $input.val().trim();
    if (!text) return;
    if (!state.ready) { addMessage("system", "Počkejte prosím na připojení."); return; }
    addMessage("user", text);
    sendToDM({ type: "user_text", text: text });
    $input.val("").css("height", "auto");
  }
  $("#btn-send").click(sendText);
  $input.on("keydown", function (e) {
    if (e.keyCode === 13 && !e.shiftKey) { e.preventDefault(); sendText(); }
  });
  $input.on("input", function () {
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight, 160) + "px";
  });

  /* === Reset ========================================================== */
  $("#btn-reset").click(function () {
    $messages.empty();
    sendToDM({ type: "reset" });
  });

  /* === Modal pro nahrání receptu ====================================== */
  function openModal() { $("#upload-modal").prop("hidden", false); $("#recipe-input").focus(); }
  function closeModal() { $("#upload-modal").prop("hidden", true); }
  $("#btn-upload, #btn-upload-2").click(openModal);
  $("#modal-close, #modal-cancel").click(closeModal);
  $("#upload-modal").click(function (e) { if (e.target === this) closeModal(); });

  $("#recipe-file").change(function (e) {
    var file = e.target.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function (ev) { $("#recipe-input").val(ev.target.result); };
    reader.readAsText(file, "UTF-8");
  });

  $(".sample").click(function () {
    var name = $(this).data("file");
    $.get("sample_recipes/" + name)
      .done(function (txt) { $("#recipe-input").val(txt); })
      .fail(function () { addMessage("system", "Ukázkový recept se nepodařilo načíst."); });
  });

  $("#modal-submit").click(function () {
    var text = $("#recipe-input").val().trim();
    if (!text) { $("#recipe-input").focus(); return; }
    if (!state.ready) { addMessage("system", "Počkejte prosím na připojení."); return; }
    sendToDM({ type: "set_recipe", text: text });
    closeModal();
  });

  /* === Připojení – stavová hláška ===================================== */
  function setConn(online, label) {
    $("#conn-status").removeClass("online offline").addClass(online ? "online" : "offline");
    $("#conn-label").text(label);
  }

  var disconnectedNotified = false;
  function onDisconnected(label) {
    state.ready = false;
    setConn(false, label);
    // Stav v hlasovém overlayi i indikátoru.
    setStatus("idle");
    if (disconnectedNotified) return;
    disconnectedNotified = true;
    // Klik na indikátor připojení = nové připojení (znovunačtení stránky).
    $("#conn-status").css("cursor", "pointer").attr("title", "Klikněte pro nové připojení")
      .off("click.reconnect").on("click.reconnect", function () { location.reload(); });
    var $m = $(
      '<div class="msg system"><div class="msg-bubble">Spojení se serverem se ' +
      'přerušilo (nejspíš kvůli nečinnosti). <button class="btn btn-primary" ' +
      'id="btn-reconnect" style="margin-left:8px">Připojit znovu</button></div></div>'
    );
    $("#messages").append($m);
    $("#btn-reconnect").click(function () { location.reload(); });
    $("#messages").scrollTop($("#messages")[0].scrollHeight);
  }

  /* === Události ze SpeechCloud ======================================== */
  speechCloud.on("ws_connected", function () { setConn(false, "spojeno, spouštím…"); });
  speechCloud.on("ws_closed",    function () { onDisconnected("odpojeno"); });
  speechCloud.on("ws_error",     function () { onDisconnected("chyba spojení"); });

  speechCloud.on("asr_ready", function () {
    state.ready = true;
    disconnectedNotified = false;
    setConn(true, "připraveno");
  });

  // Hlavní kanál: data z dialogového manažeru
  speechCloud.on("dm_receive_message", function (msg) {
    var d = msg.data || {};
    switch (d.type) {
      case "status":        setStatus(d.state); break;
      case "assistant":     addMessage("assistant", d.text); break;
      case "user_speech":   addMessage("user", d.text); $("#voice-caption").text(""); break;
      case "info":          addMessage("system", d.text); break;
      case "recipe_loaded": renderRecipe(d); break;
      case "progress":      updateProgress(d); break;
      default: console.log("Neznámá zpráva z DM:", d);
    }
  });

  // Živý přepis řeči (průběžné výsledky ASR) – jen do hlasového overlaye.
  speechCloud.on("asr_result", function (msg) {
    if (state.voiceMode && msg.partial_result) {
      $("#voice-caption").text(msg.result || "");
    }
  });

  speechCloud.on("sc_error", function (msg) {
    console.error("SpeechCloud error:", msg);
    addMessage("system", "Chyba služby: " + (msg.error || ""));
  });

  // Spuštění SpeechCloud
  setConn(false, "připojuji…");
  speechCloud.init();
});
