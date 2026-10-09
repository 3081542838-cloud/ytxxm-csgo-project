const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
const settings = vm.runInNewContext(source + '\nCS2POVRadarSettings;', {});
const configured = ['.42', 'false', 'true', '1.25', '.7', '1', '0'];
const requested = [];
const api = {GetSettingString(name) {
    requested.push(name);
    return configured[settings.names.indexOf(name)];
}, SetSettingString() {throw new Error('Must never change a setting');}};
const snapshot = settings.read(api);
assert.deepEqual(JSON.parse(JSON.stringify(snapshot)), {schema: 1, values: [.42, 0, 1, 1.25, .7, 1, 0]});
assert.deepEqual(requested, Array.from(settings.names));
assert(Object.isFrozen(snapshot) && Object.isFrozen(snapshot.values));
configured[0] = '.8';
assert.equal(snapshot.values[0], .42);
assert.equal(settings.read(api).values[0], .8);
for (const invalid of ['', undefined, 'NaN', 'Infinity', '0', '-1', '11']) {
    configured[0] = invalid;
    assert.throws(() => settings.read(api));
}
configured[0] = '.42';
configured[1] = '2';
assert.throws(() => settings.read(api));
assert.throws(() => settings.read({GetSettingFloat: () => 0}));
assert.throws(() => settings.read({GetSettingString: () => {throw new Error('Engine not ready');}}));
assert.throws(() => settings.decode([.42]));
assert(!/\.SetSetting|\.ConsoleCommand|\.Execute|\.DispatchEvent|\.Schedule/.test(source));
console.log('PASS read-only radar settings, immutable snapshots, missing settings and no writes');
