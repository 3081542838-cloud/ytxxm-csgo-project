/* Read-only local demo state. Session marker is replaced by the resource builder. */
;(function () {
    "use strict";
    let sequence = 0;
    function inspect() {
        $.Schedule(0.5, inspect);
        sequence += 1;
        try {
            const context = $.GetContextPanel();
            const state = context.GetDemoControllerState ? context.GetDemoControllerState() : null;
            const report = {
                nonce: "__CS2POV_SESSION_NONCE__", sequence: sequence, context: context.id,
                state: state ? {
                    sFileName: state.sFileName, nTick: state.nTick, bIsPaused: state.bIsPaused,
                    nObserverMode: state.nObserverMode, nSpectatingPlayerId: state.nSpectatingPlayerId,
                    bIsPlayingDemoFile: state.bIsPlayingDemoFile,
                    bIsPlayingBroadcast: state.bIsPlayingBroadcast
                } : null,
                xuid: typeof GameStateAPI !== "undefined" ? String(GameStateAPI.GetHudPlayerXuid() || "") : ""
            };
            const message = JSON.stringify(report);
            // The engine truncates large messages. Never emit a partial JSON record.
            if (message.length > 512) { $.Msg("POV_READBACK_ERROR message too long"); return; }
            $.Msg("POV_READBACK " + message);
        } catch (error) { $.Msg("POV_READBACK_ERROR unavailable state"); }
    }
    inspect();
})();
