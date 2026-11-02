"""Run panel presentation regressions against the actual script, without a browser or network."""

import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is required for panel JavaScript checks")
class PanelJavaScriptTests(unittest.TestCase):
    def test_group_statistics_follow_ids_and_current_configuration(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = vm.createContext({
  document: { getElementById: () => ({}), addEventListener: () => {} },
  fetch: () => new Promise(() => {}), setInterval: () => {},
});
vm.runInContext(fs.readFileSync('backend/static/panel.js', 'utf8'), context);
vm.runInContext(`
  snapshot = { groups: [{id:'new', name:'新增群'}, {id:'kept', name:'保留群'}] };
`, context);
const status = {
  group_conversations: {ued:'removed', 'group-1':'kept', 'group-7':'new'},
  listeners: {direct:{accepted:2},at:{accepted:3},ued:{accepted:99},
    'group-1':{accepted:4,state:'ready'},'group-7':{accepted:5,state:'ready'}},
};
context.statusFixture = status;
const note = vm.runInContext('listenerNote(statusFixture, snapshot.groups)', context);
assert.match(note, /新增群 5 条，保留群 4 条。/);
assert.ok(!note.includes('99'));
assert.ok(!note.includes('条 条'));
assert.equal(vm.runInContext("sourceName('ued', {conversation_id:'new'})", context), '新增群');
assert.equal(vm.runInContext("sourceName('group-1', {conversation_id:'removed'})", context), '已取消关注的群');
assert.match(vm.runInContext("listenerNote({}, [{id:'pending',name:'待启动群'}])", context), /待启动群 0 条（正在启动监听）/);
assert.equal(vm.runInContext('listenerNote(statusFixture, [])', context),
  '本次进程实际接收：单聊 2 条，@我 3 条。历史补读不计入实时接收；订阅就绪不代表已验证群事件投递。');
"""
        subprocess.run(
            ["node", "-e", script], cwd=str(Path(__file__).resolve().parent.parent), check=True
        )
