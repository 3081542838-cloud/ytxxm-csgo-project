const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2],'utf8');
assert(!/ConsoleCommand|PlaySound|CreatePanel|https?:|DispatchEvent|DeleteAsync/.test(source));
let scheduled = [], messages = [], unavailable = false;
const state = {sFileName:'C:/demo/中文.dem',nTick:12602,bIsPaused:true,nObserverMode:2,
    nSpectatingPlayerId:1288,bIsPlayingDemoFile:true,bIsPlayingBroadcast:false,
    TimelineEvents:Array(10000).fill('must not appear')};
const context = {id:'HudDemoController',GetDemoControllerState:()=>{
    if(unavailable)throw Error('panel invalid'); return state;
}};
vm.runInNewContext(source,{$:{GetContextPanel:()=>context,
    Schedule:(seconds,fn)=>{assert.equal(seconds,.5);scheduled.push(fn);},Msg:m=>messages.push(m)},
    GameStateAPI:{GetHudPlayerXuid:()=> '76561199198478034'}});
let report=JSON.parse(messages[0].slice('POV_READBACK '.length));
assert.equal(report.nonce,'__CS2POV_SESSION_NONCE__');
assert.equal(report.sequence,1);
assert.equal(report.xuid,'76561199198478034');
assert(!('TimelineEvents' in report.state));
assert.equal(report.state.nObserverMode,2);
state.nObserverMode=3; scheduled.shift()();
report=JSON.parse(messages.at(-1).slice('POV_READBACK '.length));
assert.equal(report.sequence,2); assert.equal(report.state.nObserverMode,3);
unavailable=true; scheduled.shift()();
assert.equal(messages.at(-1),'POV_READBACK_ERROR unavailable state');
unavailable=false; state.sFileName='x'.repeat(1000); scheduled.shift()();
assert.equal(messages.at(-1),'POV_READBACK_ERROR message too long');
state.sFileName='C:/another.dem'; scheduled.shift()();
assert.equal(JSON.parse(messages.at(-1).slice('POV_READBACK '.length)).sequence,5);
assert.equal(scheduled.length,1); // Exactly one bounded polling chain, even after errors.
console.log('PASS: read-only compact telemetry, errors, sequence and polling');
