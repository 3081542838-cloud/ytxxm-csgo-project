/* CS2 POV Helper: HUD visibility only; no console, audio or network commands. */
;(function () {
    "use strict";
    function valid(panel) { return panel && panel.IsValid(); }
    function root() {
        let panel = $.GetContextPanel();
        for (let count = 0; valid(panel) && count < 40; count += 1) {
            const parent = panel.GetParent();
            if (!valid(parent)) { break; }
            panel = parent;
        }
        return panel;
    }
    function visible(panel, value) {
        if (!valid(panel)) { return; }
        panel.visible = value;
        panel.style.opacity = value ? "1" : "0";
        panel.style.visibility = value ? "visible" : "collapse";
    }
    function byClass(panel, name, value, preserveScore) {
        if (!valid(panel) || !panel.FindChildrenWithClassTraverse) { return; }
        const children = panel.FindChildrenWithClassTraverse(name) || [];
        children.forEach(function (child) {
            if (preserveScore) {
                let parent = child;
                for (let count = 0; valid(parent) && count < 24; count += 1) {
                    if (parent.id === "ScoreAndTimeAndBomb") { return; }
                    parent = parent.GetParent();
                }
            }
            visible(child, value);
        });
    }
    function update() {
        try {
            const hud = root();
            if (valid(hud)) {
                ["HudSpecplayer", "HudSpecPlayer", "HudSpecplayerRoot", "HudSpecplayerParentContainer",
                 "HudRadar", "CSGOHudRadar", "HudDemoController", "CSGOHudDemoController"].forEach(function (id) {
                    visible(hud.FindChildTraverse(id), false);
                });
                ["HudSpecplayerParentContainer", "HudSpecplayerRoot--visible", "HudSpecplayer__Bg"].forEach(function (name) {
                    byClass(hud, name, false, false);
                });
                ["HudHealthAmmoCenter", "CSGOHudHealthAmmoCenter"].forEach(function (id) {
                    visible(hud.FindChildTraverse(id), true);
                });
                byClass(hud, "hud-HA__stroke", true, false);
                ["AvatarL_BG", "equipinfo-root", "hudteamcounter-equipmentinfo", "equipinfo__bg-container"].forEach(function (name) {
                    byClass(hud, name, false, true);
                });
                let team = 0;
                if (typeof GameStateAPI !== "undefined" && GameStateAPI.GetHudPlayerXuid && GameStateAPI.GetPlayerTeamNumber) {
                    team = Number(GameStateAPI.GetPlayerTeamNumber(GameStateAPI.GetHudPlayerXuid()));
                }
                [["TeamLargeCT", 3], ["TeamLargeT", 2]].forEach(function (side) {
                    const panel = hud.FindChildTraverse(side[0]);
                    if (!valid(panel)) { return; }
                    const ally = team === side[1];
                    if (panel.SetHasClass) {
                        panel.SetHasClass("CS2InsightPovEnemy", !ally);
                        panel.SetHasClass("CS2InsightPovAlly", ally);
                    }
                    ["healthbar-container", "AvatarL__C4", "AvatarL__DefuseKit"].forEach(function (name) {
                        byClass(panel, name, ally, false);
                    });
                });
            }
        } catch (error) { /* Map transitions can invalidate panels mid-update. */ }
        $.Schedule(0.25, update);
    }
    update();
})();
