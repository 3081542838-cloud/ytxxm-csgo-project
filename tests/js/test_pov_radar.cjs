const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const data = {schema: 1, player: '76561199198478034', demo:'C:/demo/test.dem', stride: 8, start: 0, end: 31,
    frames: [[0, [['76561199198478034', 0, 50, 50, 0, 90]]],
             [8, [['76561199198478034', 0, 51, 50, 0, 90], ['2', 2, 70, 40, 0, 0]]],
             [16, [['76561199198478034', 0, 52, 50, 0, 90], ['2', 3, 70, 40, 0, 0]]],
             [24, []]]};
class Panel {
    constructor(id, parent, classes = []) {
        this.id = id; this.parent = parent; this.children = []; this.classes = classes;
        this.style = {}; this.visible = true; this.valid = true;
        if (parent) parent.children.push(this);
    }
    IsValid() {return this.valid;}
    GetChildCount() {return this.children.length;}
    GetChild(i) {return this.children[i];}
    GetParent() {return this.parent;}
    FindChildTraverse(id) {
        for (const child of this.children) {
            if (child.id === id) return child;
            const nested = child.FindChildTraverse(id); if (nested) return nested;
        }
        return null;
    }
    FindChildrenWithClassTraverse(name) {
        return this.children.flatMap(child => (child.classes.includes(name) ? [child] : [])
            .concat(child.FindChildrenWithClassTraverse(name)));
    }
    SetImage(image) {this.image = image;}
}
const root = new Panel('Hud');
const radar = new Panel('HudRadar', root);
const round = new Panel('Radar__Round--InnerTransform', radar);
const square = new Panel('Radar__Square--InnerTransform', radar);
round.style.transform = 'engine rotated + zoomed';
square.style.transform = 'engine square';
const native = new Panel('PlayerIcon007', round);
const nativeClass = new Panel('enemy', square, ['PlayerIcons']);
const background = new Panel('Background', round);
const sound = new Panel('RI_PlayerSoundContainer', radar);
const bomb = new Panel('DroppedBomb', square);
let reads = 0;
const settings = ['.42', '0', '1', '1.25', '.7', '1', '0'];
const source = fs.readFileSync(process.argv[2], 'utf8');
const settingSource = fs.readFileSync(process.argv[3], 'utf8');
const context = {$: {CreatePanel(type, parent, id) {return new Panel(id, parent);}}};
const implementation = vm.runInNewContext(settingSource + '\n' + source + '\nCS2POVRadar;', context);
const api = {GetSettingString(name) {
    reads += 1;
    const names = ['cl_radar_scale','cl_radar_rotate','cl_radar_always_centered',
        'cl_hud_radar_scale','cl_radar_icon_scale_min','cl_radar_square_always','cl_radar_square_when_spectating'];
    return settings[names.indexOf(name)];
}};
const renderer = implementation.renderer(api, data);
assert(renderer.update(root, 8, data.player));
assert.equal(reads, 7);
assert(!native.visible && !nativeClass.visible && !sound.visible && !bomb.visible);
assert(background.visible && radar.visible);
assert.equal(round.style.transform, 'engine rotated + zoomed');
assert.equal(square.style.transform, 'engine square');
assert.equal(round.FindChildTraverse('XiamiPOVRadar_0_1').style.position, '70% 40% 0px');
assert(renderer.update(root, 16, data.player));
assert.equal(round.FindChildTraverse('XiamiPOVRadar_0_1').style.opacity, '0.55');
assert.equal(round.FindChildTraverse('XiamiPOVRadar_0_1').style.position, '70% 40% 0px');
assert(renderer.update(root, 0, data.player));
assert(!round.FindChildTraverse('XiamiPOVRadar_0_1').visible); // no future intel on rewind
native.visible = true;
assert(renderer.update(root, 0, data.player));
assert(!native.visible); // stock engine reappearance is suppressed every update
assert.equal(reads, 7); // task snapshot does not change during recording
assert(!renderer.update(root, 24, data.player) && !radar.visible); // dead POV
assert(!renderer.update(root, 8, '2') && !radar.visible); // different POV
assert(!renderer.update(root, 32, data.player) && !radar.visible); // outside clip
assert(!renderer.update(root, 8.5, data.player));
assert.deepEqual(JSON.parse(JSON.stringify(implementation.state(data, 15, data.player))), data.frames[1][1]);
const unavailable = implementation.renderer({GetSettingString: () => ''}, data);
assert(!unavailable.update(root, 8, data.player) && !radar.visible);
round.valid = false; square.valid = false;
assert(!renderer.update(root, 8, data.player) && !radar.visible);
assert(!/\.SetSetting|\.ConsoleCommand|\.Execute|\.DispatchEvent|https?:|\.Schedule/.test(source));
// Exercise the actual bootstrap: the renderer must not overrule the user's
// hide command by repeatedly showing the parent from its update loop.
round.valid = true; square.valid = true;
const controller = new Panel('HudDemoController', root);
controller.GetDemoControllerState = () => ({sFileName:data.demo,nTick:8, bIsPlayingDemoFile:true, bIsPlayingBroadcast:false, nObserverMode:2});
let display = '1';
const scheduled = [], messages = [];
const runtime = fs.readFileSync(process.argv[4], 'utf8')
    .replace('__CS2POV_RADAR_DATA__',JSON.stringify(data)).replace('__CS2POV_SESSION_NONCE__','TEST_SESSION_0001');
vm.runInNewContext(settingSource + '\n' + source + '\n' + runtime, {
    $: {GetContextPanel:()=>controller, CreatePanel:(type,parent,id)=>new Panel(id,parent),
        Schedule:(seconds,fn)=>scheduled.push(fn),Msg:message=>messages.push(message)},
    GameStateAPI:{GetHudPlayerXuid:()=>data.player},
    GameInterfaceAPI:{GetSettingString:name=>name==='cl_drawhud_force_radar'?display:api.GetSettingString(name)}
});
assert(radar.visible);
display = '-1'; scheduled.shift()(); assert(!radar.visible);
display = ''; scheduled.shift()(); assert(!radar.visible);
display = '1'; scheduled.shift()(); assert(radar.visible);
controller.GetDemoControllerState = () => ({sFileName:'C:/other/same-player.dem',nTick:8,
    bIsPlayingDemoFile:true,bIsPlayingBroadcast:false,nObserverMode:2});
scheduled.shift()(); assert(!radar.visible); // IDs/ticks can repeat across different demos
controller.GetDemoControllerState = () => {throw new Error('Map transition');};
scheduled.shift()(); assert(!radar.visible);
assert.equal(messages.length,1);
console.log('PASS radar filtering, native marker suppression, map transforms, snapshots, seeks and fail-closed');
