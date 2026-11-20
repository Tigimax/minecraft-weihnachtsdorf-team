const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {
  loadMemory,
  saveMemory,
  recordActionResult,
  decideNextAction,
  fallbackDecision,
} = require('./brain');

const MEMORY_PATH = path.join(__dirname, 'memory.json');
const hadMemory = fs.existsSync(MEMORY_PATH);
const backupMemory = hadMemory ? fs.readFileSync(MEMORY_PATH, 'utf8') : null;

async function runTests() {
  try {
    if (hadMemory) fs.unlinkSync(MEMORY_PATH);

    const mem = loadMemory();
    assert.strictEqual(mem.botName, 'Waldlaufer_Rudi', 'botName must be Waldlaufer_Rudi');
    assert.ok(typeof mem.players === 'object', 'players must be an object');
    assert.ok(Array.isArray(mem.planQueue), 'planQueue must be an array');
    assert.ok(Array.isArray(mem.actionHistory), 'actionHistory must be an array');
    assert.ok(typeof mem.learnedSkills === 'object', 'learnedSkills must be an object');
    assert.ok(typeof mem.activeProject === 'object', 'activeProject must be an object');
    assert.ok(Array.isArray(mem.personalGoals) && mem.personalGoals.length >= 4, 'personalGoals must have >= 4 defaults');

    // Legacy memory without personalGoals must backfill defaults
    delete mem.personalGoals;
    saveMemory(mem);
    const backfilledMem = loadMemory();
    assert.ok(Array.isArray(backfilledMem.personalGoals) && backfilledMem.personalGoals.length >= 4, 'legacy memory must backfill personalGoals');

    // 1. Multi-step planQueue execution without LLM calls
    mem.planQueue = [
      { actionType: 'chop_wood', target: '2', thought: 'Holz holen', chat: '' },
      { actionType: 'craft', target: 'crafting_table', thought: 'Werkbank bauen', chat: '' },
    ];
    saveMemory(mem);

    const snap = {
      timeOfDay: 'Tag',
      health: 20,
      food: 20,
      position: { x: -6070, y: 81, z: -3223 },
      homePosition: { x: -6070, y: 81, z: -3223 },
      inventory: [],
      nearbyPlayers: [{ name: 'Tigimax', distance: 6 }],
      nearbyBlocks: ['oak_log', 'grass_block'],
      nearbyEntities: [],
      distanceFromHome: 2,
    };

    const step1 = await decideNextAction(snap, null, { allowLlm: false });
    assert.strictEqual(step1.actionType, 'chop_wood', 'first queued step must run');
    assert.strictEqual(loadMemory().planQueue.length, 1, 'one queued step must remain');

    // 2. Failure verification clears planQueue and triggers anti-loop fallback to explore
    recordActionResult('chop_wood', '2', false, 'Kein Baum in Reichweite');
    const afterFail = loadMemory();
    assert.strictEqual(afterFail.planQueue.length, 0, 'failure must clear remaining planQueue');
    assert.strictEqual(afterFail.actionHistory[0].ok, false, 'failure recorded at head of actionHistory');

    const fb = fallbackDecision(snap, afterFail, null);
    assert.strictEqual(fb.plan[0].actionType, 'explore', 'anti-loop fallback must explore after failed chop_wood');

    // 3. Skill library persistence
    recordActionResult('custom_code', '', true, 'Beeren gesammelt', {
      name: 'gather_berries',
      description: 'Sammelt Beeren',
      code: 'return;',
    });
    const afterSkill = loadMemory();
    assert.ok(afterSkill.learnedSkills.gather_berries, 'learned skill must persist');
    assert.strictEqual(afterSkill.learnedSkills.gather_berries.uses, 1, 'uses counter must start at 1');

    // 4. Chat trigger planning & persistent activeDirective lock until completion
    const chatRes = await decideNextAction(
      snap,
      { username: 'Tigimax', message: 'Rudi komm mal her' },
      { allowLlm: false }
    );
    assert.strictEqual(chatRes.actionType, 'visit_player', 'chat call must route to visit_player');
    assert.ok(typeof chatRes.chat === 'string' && chatRes.chat.length > 0, 'chat reply must be non-empty');
    recordActionResult('visit_player', 'Tigimax', true, 'Tigimax besucht (2m)');
    assert.strictEqual(loadMemory().activeDirective, null, 'completed directive must clear');

    const hutCmd = await decideNextAction(
      snap,
      { username: 'Tigimax', message: '@rudi bitte in die hütte gehen' },
      { allowLlm: false }
    );
    assert.strictEqual(hutCmd.actionType, 'build_hut', 'hut chat command must route to build_hut');
    recordActionResult('build_hut', '', false, 'Bei Försterhütte (35m Abstand)');
    const keepDirective = await decideNextAction(snap, null, { allowLlm: false });
    assert.strictEqual(keepDirective.actionType, 'build_hut', 'failed step must keep activeDirective instead of mining');
    recordActionResult('build_hut', '', true, 'Bei Försterhütte (2m Abstand)');
    assert.strictEqual(loadMemory().activeDirective, null, 'arriving at hut must clear activeDirective');

    console.log('SELF_CHECK_OK');
  } finally {
    if (hadMemory) {
      fs.writeFileSync(MEMORY_PATH, backupMemory, 'utf8');
    } else if (fs.existsSync(MEMORY_PATH)) {
      fs.unlinkSync(MEMORY_PATH);
    }
  }
}

runTests().catch((err) => {
  console.error(err);
  process.exit(1);
});
