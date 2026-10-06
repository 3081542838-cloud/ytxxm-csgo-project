const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Panel {
  constructor(id, parent = null, classes = []) {
    this.id = id; this.parent = parent; this.classes = new Set(classes);
    this.children = []; this.visible = true; this.style = {}; this.valid = true;
    if (parent) parent.children.push(this);
  }
  IsValid() { return this.valid; }
  GetParent() { return this.parent; }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
  FindChildTraverse(id) { return this.all().find(panel => panel.id === id); }
  FindChildrenWithClassTraverse(name) { return this.all().filter(panel => panel.classes.has(name)); }
  SetHasClass(name, on) { if (on) this.classes.add(name); else this.classes.delete(name); }
}
const hud = new Panel('HudRoot');
const controller = new Panel('HudDemoController', hud);
const health = new Panel('HudHealthAmmoCenter', hud); health.value = '87 HP / 78 armor / 30 ammo';
const spectator = new Panel('HudSpecplayer', hud);
const radar = new Panel('HudRadar', hud);
const score = new Panel('ScoreAndTimeAndBomb', hud);
const scoreBackground = new Panel('score-bg', score, ['equipinfo__bg-container']);
const equipment = new Panel('spectator-equipment', hud, ['equipinfo-root']);
const ct = new Panel('TeamLargeCT', hud), t = new Panel('TeamLargeT', hud);
const ctHP = new Panel('ct-health', ct, ['healthbar-container']);
const tHP = new Panel('t-health', t, ['healthbar-container']);
const killfeed = new Panel('HudDeathNotice', hud); killfeed.value = 'native kill feed';
let team = 3, forbiddenCalls = 0;
const scheduled = [];
const forbidden = () => { forbiddenCalls++; throw new Error('forbidden operation'); };
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  $: { GetContextPanel: () => controller, Schedule: (time, fn) => { assert.equal(time, 0.25); scheduled.push(fn); }, PlaySound: forbidden, CreatePanel: forbidden },
  GameStateAPI: { GetHudPlayerXuid: () => '76561198000000000', GetPlayerTeamNumber: () => team },
  GameInterfaceAPI: new Proxy({}, { get: forbidden }),
});
assert.equal(spectator.visible, false);
assert.equal(radar.visible, false);
assert.equal(controller.visible, false);
assert.equal(health.visible, true);
assert.equal(health.value, '87 HP / 78 armor / 30 ammo');
assert.equal(scoreBackground.visible, true);
assert.equal(equipment.visible, false);
assert.equal(killfeed.visible, true);
assert.equal(killfeed.value, 'native kill feed');
assert.equal(ctHP.visible, true); assert.equal(tHP.visible, false);
team = 2; scheduled.shift()();
assert.equal(ctHP.visible, false); assert.equal(tHP.visible, true);
team = 0; scheduled.shift()();
assert.equal(ctHP.visible, false); assert.equal(tHP.visible, false);
hud.valid = false; scheduled.shift()();
assert.equal(scheduled.length, 1);
assert.equal(forbiddenCalls, 0);
console.log('HUD visibility, native values, score/killfeed preservation, team switch, invalid state and no commands/audio/network: PASS');
