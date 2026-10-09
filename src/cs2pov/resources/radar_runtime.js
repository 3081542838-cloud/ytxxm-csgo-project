/* Controlled session bootstrap. The builder embeds validated POV data only. */
;(function () {
    "use strict";
    const data = __CS2POV_RADAR_DATA__;
    const renderer = CS2POVRadar.renderer(typeof GameInterfaceAPI !== "undefined" ? GameInterfaceAPI : null, data);
    let reported = false;
    function update() {
        try {
            let root = $.GetContextPanel();
            const state = root.GetDemoControllerState ? root.GetDemoControllerState() : null;
            for (let count = 0; root && root.IsValid() && count < 40; count += 1) {
                const parent = root.GetParent();
                if (!parent || !parent.IsValid()) { break; }
                root = parent;
            }
            const player = typeof GameStateAPI !== "undefined" ? String(GameStateAPI.GetHudPlayerXuid() || "") : "";
            const display = typeof GameInterfaceAPI !== "undefined" && GameInterfaceAPI.GetSettingString
                ? GameInterfaceAPI.GetSettingString("cl_drawhud_force_radar") : "";
            const enabled = typeof display === "string" && display.trim() !== "" && Number(display) === 1;
            const sameDemo = state && typeof state.sFileName === "string"
                && state.sFileName.replace(/\\/g, "/").toLowerCase() === data.demo.replace(/\\/g, "/").toLowerCase();
            const tick = enabled && sameDemo && state && state.bIsPlayingDemoFile && !state.bIsPlayingBroadcast && state.nObserverMode === 2
                ? state.nTick : -1;
            if (renderer.update(root, tick, player) && !reported) {
                $.Msg("POV_RADAR_SETTINGS " + JSON.stringify({nonce: "__CS2POV_SESSION_NONCE__", settings: renderer.settings()}));
                reported = true;
            }
        } catch (error) { renderer.clear(); }
        $.Schedule(0.125, update);
    }
    update();
})();
