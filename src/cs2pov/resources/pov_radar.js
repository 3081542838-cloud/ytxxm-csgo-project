/* Session-only POV markers on the engine's unchanged radar map transforms.
 * No game commands, input hooks, downloads, audio changes or setting writes.
 */
const CS2POVRadar = (function () {
    "use strict";
    function valid(panel) { return panel && panel.IsValid(); }
    function visibility(panel, show) {
        if (!valid(panel)) { return; }
        panel.visible = show;
        panel.style.opacity = show ? "1" : "0";
        panel.style.visibility = show ? "visible" : "collapse";
    }
    function state(data, tick, player) {
        if (!Number.isInteger(tick) || tick < data.start || tick > data.end || String(player) !== data.player) {
            return [];
        }
        let left = 0, right = data.frames.length;
        while (left < right) {
            const middle = Math.floor((left + right) / 2);
            if (data.frames[middle][0] <= tick) { left = middle + 1; } else { right = middle; }
        }
        const frame = data.frames[left - 1];
        return frame && tick - frame[0] < data.stride ? frame[1] : [];
    }
    function renderer(api, data) {
        let frozenSettings = null;
        let lastRadar = null;
        const panels = [];
        function clear() {
            visibility(lastRadar, false);
            panels.forEach(function (panel) { visibility(panel, false); });
        }
        function update(hud, tick, player) {
            clear();
            const radar = valid(hud) && (hud.FindChildTraverse("HudRadar") || hud.FindChildTraverse("CSGOHudRadar"));
            if (!valid(radar)) { return false; }
            lastRadar = radar;
            // Hide the complete native radar before inspecting anything. A
            // missing transform/settings/POV must not reveal spectator icons.
            visibility(radar, false);
            try {
                if (!frozenSettings) { frozenSettings = CS2POVRadarSettings.read(api); }
                const markers = state(data, tick, player);
                if (!markers.some(function (marker) { return marker[1] === 0; })) { return false; }
                const transforms = ["Radar__Round--InnerTransform", "Radar__Square--InnerTransform"]
                    .map(function (id) { return radar.FindChildTraverse(id); }).filter(valid);
                if (!transforms.length || !radar.FindChildrenWithClassTraverse) { return false; }
                // Native player/sound/bomb markers carry observer information.
                // Keep background, mask and engine transforms untouched.
                ["PlayerIcons", "PlayerSound", "BombIcons"].forEach(function (name) {
                    (radar.FindChildrenWithClassTraverse(name) || []).forEach(function (panel) { visibility(panel, false); });
                });
                ["RI_PlayerSoundContainer", "RI_BombDefuserPackage", "RI_DefuserPackage",
                 "DroppedBomb", "DefuserIconDropped", "DefuserIconPackage"].forEach(function (id) {
                    visibility(radar.FindChildTraverse(id), false);
                });
                transforms.forEach(function (transform) {
                    if (!transform.GetChildCount || !transform.GetChild) { throw new Error("Unknown radar layout"); }
                    for (let i = 0; i < transform.GetChildCount(); i += 1) {
                        const child = transform.GetChild(i);
                        if (valid(child) && String(child.id || "").indexOf("PlayerIcon") === 0) {
                            visibility(child, false);
                        }
                    }
                });
                transforms.forEach(function (transform, hostIndex) {
                    markers.forEach(function (marker, markerIndex) {
                        const id = "XiamiPOVRadar_" + hostIndex + "_" + markerIndex;
                        let panel = transform.FindChildTraverse(id);
                        if (!valid(panel)) {
                            panel = $.CreatePanel("Image", transform, id);
                            panel.SetImage("s2r://panorama/images/hud/radar/icon-on-map_png.vtex");
                            panel.hittest = false;
                            panels.push(panel);
                        }
                        panel.style.position = marker[2] + "% " + marker[3] + "% 0px";
                        panel.style.width = "9px";
                        panel.style.height = "9px";
                        panel.style.marginLeft = "-4.5px";
                        panel.style.marginTop = "-4.5px";
                        panel.style.washColor = marker[1] === 0 ? "#ffffff" : marker[1] === 1 ? "#66baff" : "#ff4545";
                        panel.style.transform = "rotateZ(" + (90 - marker[5]) + "deg)";
                        visibility(panel, true);
                        if (marker[1] === 3) { panel.style.opacity = "0.55"; }
                    });
                });
                visibility(radar, true);
                return true;
            } catch (error) { clear(); visibility(radar, false); return false; }
        }
        return {update: update, clear: clear, settings: function () { return frozenSettings; }};
    }
    return {state: state, renderer: renderer};
})();
