/* Read actual engine settings, without console commands or guessed defaults.
 * Loaded only by the radar session builder, not by ordinary hidden-radar tasks.
 */
const CS2POVRadarSettings = (function () {
    "use strict";
    const names = [
        "cl_radar_scale", "cl_radar_rotate", "cl_radar_always_centered",
        "cl_hud_radar_scale", "cl_radar_icon_scale_min",
        "cl_radar_square_always", "cl_radar_square_when_spectating"
    ];
    const switches = [1, 2, 5, 6];
    function decode(values) {
        if (!Array.isArray(values) || values.length !== names.length) {
            throw new Error("Incomplete radar settings");
        }
        const copy = values.map(function (value, index) {
            if (typeof value !== "number" || !Number.isFinite(value)) {
                throw new Error("Invalid radar setting");
            }
            if (switches.indexOf(index) >= 0 ? value !== 0 && value !== 1 : value <= 0 || value > 10) {
                throw new Error("Invalid radar setting range");
            }
            return value;
        });
        return Object.freeze({schema: 1, values: Object.freeze(copy)});
    }
    function read(api) {
        // A numeric getter may silently return zero for a missing cvar. Require
        // the string getter so absent settings cannot masquerade as false.
        if (!api || typeof api.GetSettingString !== "function") {
            throw new Error("Radar setting reader unavailable");
        }
        return decode(names.map(function (name) {
            const value = api.GetSettingString(name);
            if (typeof value !== "string" || !value.trim()) {
                throw new Error("Radar setting unavailable: " + name);
            }
            const text = value.trim().toLowerCase();
            return text === "true" ? 1 : text === "false" ? 0 : Number(text);
        }));
    }
    return Object.freeze({read: read, decode: decode, names: Object.freeze(names)});
})();
