#!/usr/bin/env node
/**
 * Weihnachtsdorf 7 - Autonomer Slice-of-Life Foerster-Bot (Waldlaufer_Rudi)
 */

const mineflayer = require('mineflayer');
const { pathfinder, Movements, goals } = require('mineflayer-pathfinder');
const collectBlock = require('mineflayer-collectblock').plugin;
const pvp = require('mineflayer-pvp').plugin;
const Vec3 = require('vec3');
const { loadMemory, saveMemory, recordActionResult, decideNextAction } = require('./brain');

const BOT_HOST = process.env.MC_BOT_HOST || '192.168.1.148';
const BOT_PORT = parseInt(process.env.MC_BOT_PORT || '25565', 10);
const BOT_NAME = process.env.MC_BOT_NAME || 'Waldlaufer_Rudi';
const BOT_VERSION = process.env.MC_BOT_VERSION || '1.21.8';
const THINK_INTERVAL_MS = parseInt(process.env.FOERSTER_THINK_MS || '3000', 10);
const LLM_MIN_INTERVAL_MS = parseInt(process.env.FOERSTER_LLM_MS || '8000', 10);

const FORBIDDEN_ITEMS = new Set(['tnt', 'lava_bucket', 'flint_and_steel', 'fire_charge', 'end_crystal', 'respawn_anchor']);
const SAFE_NATURAL_BLOCKS = [
  'log', 'leaves', 'snow', 'powder_snow', 'dirt', 'grass', 'podzol', 'stone', 'cobblestone',
  'coal_ore', 'iron_ore', 'copper_ore', 'deepslate', 'sweet_berry', 'sapling', 'dandelion', 'poppy',
  'fern', 'tall_grass', 'dead_bush', 'gravel', 'sand', 'clay', 'ice', 'packed_ice'
];

let bot = null;
let mcData = null;
let isBusy = false;
let isBusySince = 0;
let activeStepId = 0;
let pendingChatTrigger = null;
let assistUntil = 0;
let recentChat = [];
let loopTimer = null;
let reflexTimer = null;
let lastChatReplyAt = 0;
let lastLlmAt = 0;
let pvpStartedAt = 0;
let pvpBusy = false;
let spawnGen = 0;
let lastHurtAt = 0;
let shelterBusy = false;
const unreachableLogs = new Map(); // "x,y,z" -> expiry timestamp (ms)
const UNREACHABLE_LOG_TTL_MS = 10 * 60 * 1000;
const logKey = (p) => `${p.x},${p.y},${p.z}`;
const isHutLog = (p, hut) => Boolean(hut && Math.abs(p.x - hut.x) <= 12 && Math.abs(p.z - hut.z) <= 12 && Math.abs(p.y - hut.y) <= 8);
const SCAFFOLD_ITEMS = new Set(['dirt', 'cobblestone', 'cobbled_deepslate', 'gravel', 'sand']);
const SOFT_SOIL_BLOCKS = new Set(['dirt', 'grass_block', 'podzol', 'coarse_dirt', 'rooted_dirt', 'gravel', 'sand']);
const WATER_NAMES = new Set(['water', 'flowing_water']);
const LAVA_NAMES = new Set(['lava', 'flowing_lava']);

function log(msg) {
  console.log(`[${new Date().toISOString()}] [🌲 ${BOT_NAME}] ${msg}`);
}

function isInsideProtectedHome(pos, radius = 8) {
  const hut = loadMemory().hutLocation;
  if (!hut || !pos) return false;
  return (
    Math.abs(pos.x - hut.x) <= radius &&
    Math.abs(pos.z - hut.z) <= radius &&
    pos.y >= hut.y - 2 &&
    pos.y <= hut.y + 10
  );
}

function installSafetyGuardrails(b) {
  const origDig = b.dig.bind(b);
  b.dig = async (block, forceLook, digFace) => {
    if (!block) return;
    const name = block.name || '';
    const isNatural = SAFE_NATURAL_BLOCKS.some((k) => name.includes(k));
    const isAssistActive = Date.now() < assistUntil;
    if (!isAssistActive && isInsideProtectedHome(block.position, 8) && !name.includes('snow')) {
      log(`Schutzfilter: Schütze mein Haus vor Abbau von '${name}' an ${block.position}`);
      return;
    }
    if (!isNatural && !isAssistActive) {
      log(`Schutzfilter: Verhindere Abbau von Spieler-Block '${name}' an ${block.position}`);
      return;
    }
    return origDig(block, forceLook, digFace);
  };

  const origPlace = b.placeBlock.bind(b);
  b.placeBlock = async (refBlock, faceVec) => {
    const held = b.heldItem?.name || '';
    if (FORBIDDEN_ITEMS.has(held)) {
      log(`Schutzfilter: Verbotenes Item '${held}' blockiert!`);
      return;
    }
    return origPlace(refBlock, faceVec);
  };
}

function collectWorldSnapshot(b) {
  if (!b || !b.entity) return null;
  const pos = b.entity.position;
  const timeTicks = getTimeOfDayTicks(b);
  const timeOfDay = timeTicks < 12000 ? 'Tag' : (timeTicks < 13800 ? 'Dämmerung' : 'Nacht');
  const weather = b.isRaining ? 'Schneefall / Regen' : 'Klar';

  const invMap = {};
  for (const item of b.inventory.items()) {
    invMap[item.name] = (invMap[item.name] || 0) + item.count;
  }
  const inventory = Object.entries(invMap).map(([k, v]) => `${k} x${v}`);

  const nearbyPlayers = [];
  for (const [pName, pObj] of Object.entries(b.players)) {
    if (pName === b.username || !pObj.entity) continue;
    const dist = Math.round(pos.distanceTo(pObj.entity.position));
    if (dist <= 48) {
      nearbyPlayers.push({
        name: pName,
        distance: dist,
        heldItem: pObj.entity.heldItem?.name || 'nichts'
      });
    }
  }
  nearbyPlayers.sort((a, b2) => a.distance - b2.distance);

  const nearbyEntities = [];
  for (const ent of Object.values(b.entities)) {
    if (!ent || ent === b.entity || ent.type === 'player' || !ent.position) continue;
    const dist = Math.round(pos.distanceTo(ent.position));
    if (dist <= 24 && ent.name) {
      nearbyEntities.push({ name: ent.name, distance: dist });
    }
  }

  const blockSet = new Set();
  for (let dx = -8; dx <= 8; dx += 2) {
    for (let dy = -2; dy <= 4; dy += 2) {
      for (let dz = -8; dz <= 8; dz += 2) {
        const blk = b.blockAt(pos.offset(dx, dy, dz));
        if (blk && blk.name !== 'air' && blk.name !== 'cave_air' && blk.name !== 'dirt' && blk.name !== 'stone') {
          blockSet.add(blk.name);
        }
      }
    }
  }

  return {
    position: { x: Math.round(pos.x), y: Math.round(pos.y), z: Math.round(pos.z) },
    health: Math.round(b.health || 20),
    food: Math.round(b.food || 20),
    timeOfDay,
    weather,
    inventory,
    nearbyPlayers: nearbyPlayers.slice(0, 8),
    nearbyEntities: nearbyEntities.slice(0, 8),
    nearbyBlocks: Array.from(blockSet).slice(0, 20),
    recentChat: recentChat.slice(-6)
  };
}

function withTimeout(promise, ms = 4000) {
  return Promise.race([
    promise.catch((err) => ({ _err: err?.message || 'err' })),
    new Promise((r) => setTimeout(() => r({ _timeout: true }), ms))
  ]);
}

function buildSafeMovements(b, fast = false) {
  const moves = new Movements(b, mcData);
  moves.canDig = !fast;
  moves.allow1by1towers = !fast;
  moves.canOpenDoors = true;
  moves.allowParkour = true;
  moves.allowSprinting = true;
  moves.maxDropDown = 4;
  for (const hazard of ['powder_snow', 'sweet_berry_bush', 'cactus']) {
    if (mcData.blocksByName?.[hazard]) {
      moves.blocksToAvoid.add(mcData.blocksByName[hazard].id);
    }
  }
  // Only dig natural terrain/snow/leaves; never player builds
  moves.blocksCantBreak = new Set();
  for (const [name, blk] of Object.entries(mcData.blocksByName || {})) {
    if (!SAFE_NATURAL_BLOCKS.some((k) => name.includes(k))) {
      moves.blocksCantBreak.add(blk.id);
    }
  }
  return moves;
}

async function moveToGoal(b, goal, timeoutMs = 15000, fast = false) {
  if (!b?.pathfinder || !mcData) return;
  const startPos = b.entity.position.clone();
  try {
    b.pathfinder.setMovements(buildSafeMovements(b, fast));
    if (b.entity?.isInWater) b.setControlState('jump', true);
    const res = await withTimeout(b.pathfinder.goto(goal), timeoutMs);
    b.setControlState('jump', false);
    b.pathfinder.setGoal(null);
    if (res && (res._err || res._timeout) && b.entity.position.distanceTo(startPos) < 2.0) {
      await tryUnstuck(b);
    }
  } catch (e) {
    try { b.pathfinder.setGoal(null); } catch (_) {}
  }
}

async function moveNear(b, targetVec, range = 2, timeoutMs = 14000) {
  return moveToGoal(b, new goals.GoalNear(targetVec.x, targetVec.y, targetVec.z, range), timeoutMs);
}

function isNaturalDiggable(blk) {
  return Boolean(
    blk &&
    SAFE_NATURAL_BLOCKS.some((k) => blk.name.includes(k)) &&
    !WATER_NAMES.has(blk.name) &&
    !LAVA_NAMES.has(blk.name)
  );
}

function hasLavaNearby(b, center) {
  for (let dx = -1; dx <= 1; dx++) {
    for (let dy = -1; dy <= 2; dy++) {
      for (let dz = -1; dz <= 1; dz++) {
        if (LAVA_NAMES.has(b.blockAt(center.offset(dx, dy, dz))?.name)) return true;
      }
    }
  }
  return false;
}

function pickStaircaseDir(b, cp, targetVec) {
  const dirs = [[1, 0], [-1, 0], [0, 1], [0, -1]];
  let best = null;
  let bestScore = -Infinity;
  const tx = targetVec ? targetVec.x - cp.x : 0;
  const tz = targetVec ? targetVec.z - cp.z : 0;
  const tlen = Math.hypot(tx, tz) || 1;
  const hasPick = b.inventory.items().some((i) => i.name.includes('pickaxe'));

  for (const [dx, dz] of dirs) {
    const mid = b.blockAt(cp.offset(dx, 1, dz));
    const top = b.blockAt(cp.offset(dx, 2, dz));
    const flr = b.blockAt(cp.offset(dx, 0, dz));
    if (!mid || !top || hasLavaNearby(b, cp.offset(dx, 1, dz))) continue;
    if (!isPassableBlock(mid) && !WATER_NAMES.has(mid.name) && !isNaturalDiggable(mid)) continue;
    if (!isPassableBlock(top) && !WATER_NAMES.has(top.name) && !isNaturalDiggable(top)) continue;

    let score = targetVec ? ((dx * tx + dz * tz) / tlen) * 8 : 0;
    for (const blk of [mid, top]) {
      if (isPassableBlock(blk)) score += 6;
      else if (WATER_NAMES.has(blk.name)) score -= 18;
      else if (SOFT_SOIL_BLOCKS.has(blk.name) || blk.name.includes('snow') || blk.name.includes('leaves')) score += 3;
      else if (!hasPick) score -= 8;
    }
    if (isSolidGroundBlock(flr)) score += 5;
    // Prefer open sky ahead
    let skyAir = 0;
    for (let y = 3; y <= 8; y++) {
      if (isPassableBlock(b.blockAt(cp.offset(dx, y, dz)))) skyAir++;
    }
    score += skyAir;
    if (score > bestScore) {
      bestScore = score;
      best = [dx, dz];
    }
  }
  return best;
}

async function placeScaffoldInto(b, targetPos) {
  const cur = b.blockAt(targetPos);
  if (!cur || isSolidGroundBlock(cur)) return isSolidGroundBlock(cur);
  const item = b.inventory.items().find((i) => SCAFFOLD_ITEMS.has(i.name));
  if (!item) return false;
  for (const f of [new Vec3(0, -1, 0), new Vec3(1, 0, 0), new Vec3(-1, 0, 0), new Vec3(0, 0, 1), new Vec3(0, 0, -1)]) {
    const ref = b.blockAt(targetPos.plus(f));
    if (isSolidGroundBlock(ref)) {
      try {
        await b.equip(item, 'hand');
        await b.lookAt(targetPos.offset(0.5, 0.5, 0.5), true);
        await withTimeout(b.placeBlock(ref, f.scaled(-1)), 1500);
        if (isSolidGroundBlock(b.blockAt(targetPos))) return true;
      } catch (e) {}
    }
  }
  return false;
}

async function clearBlockForStaircase(b, blk) {
  if (!blk || isPassableBlock(blk)) return true;
  if (WATER_NAMES.has(blk.name)) {
    await placeScaffoldInto(b, blk.position);
    const after = b.blockAt(blk.position);
    if (after && isNaturalDiggable(after)) await withTimeout(b.dig(after), 3000);
    return !LAVA_NAMES.has(b.blockAt(blk.position)?.name);
  }
  if (!isNaturalDiggable(blk)) return false;
  if (!SOFT_SOIL_BLOCKS.has(blk.name) && !blk.name.includes('snow') && !blk.name.includes('leaves')) {
    await equipItemByName(b, 'pickaxe');
  }
  await withTimeout(b.dig(blk), 8500);
  return isPassableBlock(b.blockAt(blk.position));
}

/**
 * Deterministically dig a 1-wide staircase upward toward open sky or targetVec (survival only).
 */
async function digStaircaseToSurface(b, targetVec = null, maxSteps = 14) {
  if (!b?.entity) return false;
  const startY = Math.round(b.entity.position.y);
  log(`Grabe Treppe nach oben (Start Y=${startY})...`);

  if (!b.inventory.items().some((i) => SCAFFOLD_ITEMS.has(i.name))) {
    const p0 = b.entity.position.floored();
    let dug = 0;
    for (let dy = 1; dy >= 0 && dug < 2; dy--) {
      for (const [dx, dz] of [[-1, 0], [1, 0], [0, -1], [0, 1]]) {
        if (dug >= 2) break;
        const blk = b.blockAt(p0.offset(dx, dy, dz));
        if (blk && SOFT_SOIL_BLOCKS.has(blk.name)) {
          await withTimeout(b.dig(blk), 2500);
          dug++;
        }
      }
    }
    if (dug > 0) {
      await new Promise((r) => setTimeout(r, 250));
      await collectNearbyDrops(b);
    }
  }

  for (let step = 0; step < maxSteps; step++) {
    const cp = b.entity.position.floored();
    if (cp.y >= 72) break;
    if (hasLavaNearby(b, cp)) break;
    if (targetVec && step >= 2) {
      const pRes = b.pathfinder.getPathTo(buildSafeMovements(b, false), new goals.GoalNear(targetVec.x, targetVec.y, targetVec.z, 2));
      if (pRes?.status === 'success') break;
    }

    const dir = pickStaircaseDir(b, cp, targetVec);
    if (!dir) break;
    const [dx, dz] = dir;

    // Clear headroom above current pos, then next step top & feet
    if (!(await clearBlockForStaircase(b, b.blockAt(cp.offset(0, 2, 0))))) break;
    if (!(await clearBlockForStaircase(b, b.blockAt(cp.offset(dx, 2, dz))))) break;
    if (!(await clearBlockForStaircase(b, b.blockAt(cp.offset(dx, 1, dz))))) break;

    const floorPos = cp.offset(dx, 0, dz);
    if (!isSolidGroundBlock(b.blockAt(floorPos))) {
      await placeScaffoldInto(b, floorPos);
      if (!isSolidGroundBlock(b.blockAt(floorPos))) break;
    }

    try { await b.lookAt(cp.offset(dx + 0.5, 1.5, dz + 0.5), true); } catch (e) {}
    b.setControlState('jump', true);
    b.setControlState('forward', true);
    const deadline = Date.now() + (b.entity.isInWater ? 1400 : 900);
    while (Date.now() < deadline && b.entity) {
      if (b.entity.position.y >= cp.y + 0.9 && Math.hypot(b.entity.position.x - (cp.x + dx + 0.5), b.entity.position.z - (cp.z + dz + 0.5)) < 0.7) {
        break;
      }
      await new Promise((r) => setTimeout(r, 80));
    }
    b.setControlState('jump', false);
    b.setControlState('forward', false);
    await new Promise((r) => setTimeout(r, 120));
    if (step % 3 === 2) await collectNearbyDrops(b);
  }

  log(`Treppen-Aufstieg beendet bei Y=${Math.round(b.entity.position.y)}.`);
  return b.entity.position.y > startY;
}

async function tryUnstuck(b) {
  if (!b?.entity) return;
  const p = b.entity.position.floored();
  const feet = b.blockAt(p);
  const head = b.blockAt(p.offset(0, 1, 0));

  if (b.entity.isInWater || feet?.name === 'water' || head?.name === 'water') {
    let shore = null;
    let bestD = Infinity;
    for (let dx = -6; dx <= 6; dx++) {
      for (let dz = -6; dz <= 6; dz++) {
        for (let dy = 0; dy <= 2; dy++) {
          const c = p.offset(dx, dy, dz);
          if (isSolidGroundBlock(b.blockAt(c)) && isPassableBlock(b.blockAt(c.offset(0, 1, 0))) && isPassableBlock(b.blockAt(c.offset(0, 2, 0)))) {
            const d = dx * dx + dz * dz;
            if (d < bestD) { bestD = d; shore = c; }
          }
        }
      }
    }
    if (shore) {
      log(`Schwimme ans Ufer bei ${shore}...`);
      try { await b.lookAt(shore.offset(0.5, 1.5, 0.5), true); } catch (e) {}
    }
    b.setControlState('jump', true);
    b.setControlState('forward', true);
    b.setControlState('sprint', true);
    await new Promise((r) => setTimeout(r, 1400));
    b.setControlState('sprint', false);
    b.setControlState('jump', false);
    b.setControlState('forward', false);
    return;
  }

  if (feet && (feet.name === 'powder_snow' || feet.name === 'snow')) {
    await withTimeout(b.dig(feet), 2000);
  }
  if (head && head.name !== 'air' && head.name !== 'cave_air' && SAFE_NATURAL_BLOCKS.some((k) => head.name.includes(k))) {
    await withTimeout(b.dig(head), 3500);
  }

  // Gather a few natural soil blocks (survival-only) when empty so we can pillar/pathfinder-tower out of pits
  const isScaffoldItem = (i) => ['dirt', 'cobblestone', 'cobbled_deepslate', 'gravel', 'sand'].includes(i.name);
  const softSoilNames = new Set(['dirt', 'grass_block', 'podzol', 'coarse_dirt', 'rooted_dirt', 'gravel', 'sand']);
  if (!b.inventory.items().some(isScaffoldItem)) {
    let dug = 0;
    for (let dy = 1; dy >= 0 && dug < 3; dy--) {
      for (const [dx, dz] of [[-1, 0], [1, 0], [0, -1], [0, 1]]) {
        if (dug >= 3) break;
        const blk = b.blockAt(p.offset(dx, dy, dz));
        if (blk && softSoilNames.has(blk.name)) {
          await withTimeout(b.dig(blk), 2500);
          dug++;
        }
      }
    }
    if (dug > 0) {
      await new Promise((r) => setTimeout(r, 250));
      await collectNearbyDrops(b);
    }
  }

  // Pillar up 1-2 blocks if headroom is clear (or diggable natural block) to escape a pit
  for (let step = 0; step < 2; step++) {
    const scaffold = b.inventory.items().find(isScaffoldItem);
    if (!scaffold) break;
    const cp = b.entity.position.floored();
    const below = b.blockAt(cp.offset(0, -1, 0));
    const head1 = b.blockAt(cp.offset(0, 1, 0));
    const head2 = b.blockAt(cp.offset(0, 2, 0));
    if (!isSolidGroundBlock(below) || !isPassableBlock(head1)) break;
    if (head2 && !isPassableBlock(head2)) {
      if (!softSoilNames.has(head2.name)) break;
      await withTimeout(b.dig(head2), 2500);
      if (!isPassableBlock(b.blockAt(cp.offset(0, 2, 0)))) break;
    }
    try {
      await b.equip(scaffold, 'hand');
      await b.lookAt(below.position.offset(0.5, 1, 0.5), true);
      b.setControlState('jump', true);
      await new Promise((r) => setTimeout(r, 260));
      await withTimeout(b.placeBlock(below, new Vec3(0, 1, 0)), 1500);
    } catch (e) {
      break;
    } finally {
      b.setControlState('jump', false);
    }
    await new Promise((r) => setTimeout(r, 200));
  }

  const openDir = pickBestEscapePoint(b, b.entity.position, b.entity.position.offset(0, 0, -1));
  try { await b.lookAt(new Vec3(openDir.x, b.entity.position.y + 1, openDir.z), true); } catch (e) {}
  b.setControlState('jump', true);
  b.setControlState('forward', true);
  b.setControlState('sprint', true);
  await new Promise((r) => setTimeout(r, 900));
  b.setControlState('sprint', false);
  b.setControlState('jump', false);
  b.setControlState('forward', false);
}

async function equipItemByName(b, partialName, dest = 'hand') {
  const item = b.inventory.items().find((i) => i.name.includes(partialName));
  if (item) {
    await withTimeout(b.equip(item, dest), 2500);
    return item;
  }
  return null;
}

const EDIBLE_ITEMS = [
  'cooked_beef', 'cooked_porkchop', 'cooked_chicken', 'cooked_mutton', 'cooked_rabbit',
  'cooked_salmon', 'cooked_cod', 'baked_potato', 'bread', 'golden_carrot', 'golden_apple',
  'rabbit_stew', 'pumpkin_pie', 'cookie', 'apple', 'sweet_berries', 'carrot', 'melon_slice',
  'glow_berries', 'dried_kelp', 'beef', 'porkchop', 'chicken', 'mutton', 'rabbit', 'potato'
];

async function autoEatIfHungry(b, force = false) {
  if (!b || b.food >= (force ? 20 : 18)) return false;
  const items = b.inventory.items();
  const food = EDIBLE_ITEMS.map((name) => items.find((i) => i.name === name)).find(Boolean);
  if (!food) return false;
  try {
    await withTimeout(b.equip(food, 'hand'), 2000);
    await withTimeout(b.consume(), 3000);
    log(`Hab ${food.name} gegessen (Hunger: ${b.food}/20).`);
    return true;
  } catch (e) {
    return false;
  }
}

const HOSTILE_MOBS = new Set([
  'zombie', 'husk', 'drowned', 'skeleton', 'stray', 'bogged',
  'creeper', 'spider', 'cave_spider', 'witch', 'pillager', 'vindicator', 'phantom', 'slime'
]);
const RANGED_MOBS = new Set(['skeleton', 'stray', 'bogged', 'witch', 'pillager']);

let lastThreatEscapeAt = 0;
let lastEscapeGoalAt = 0;
let lastEscapeLogAt = 0;
let escapeActive = false;

function isPassableBlock(blk) {
  if (!blk) return false;
  return ['air', 'cave_air', 'snow', 'short_grass', 'tall_grass', 'fern'].includes(blk.name);
}

function isSolidGroundBlock(blk) {
  return Boolean(blk && blk.boundingBox === 'block' && !['water', 'lava', 'powder_snow'].includes(blk.name));
}

function getTimeOfDayTicks(b) {
  const raw = b?.time?.timeOfDay;
  if (typeof raw !== 'number' || Number.isNaN(raw)) return 6000;
  return ((raw % 24000) + 24000) % 24000;
}

function isNightTime(b) {
  const t = getTimeOfDayTicks(b);
  return t >= 12200 && t <= 23300;
}

function isShelteredInPlace(b) {
  if (!b?.entity) return false;
  const p = b.entity.position.floored();
  if (!isSolidGroundBlock(b.blockAt(p.offset(0, 2, 0)))) return false;
  const eyeWalls = [
    b.blockAt(p.offset(1, 1, 0)),
    b.blockAt(p.offset(-1, 1, 0)),
    b.blockAt(p.offset(0, 1, 1)),
    b.blockAt(p.offset(0, 1, -1))
  ].filter(isSolidGroundBlock).length;
  const feetWalls = [
    b.blockAt(p.offset(1, 0, 0)),
    b.blockAt(p.offset(-1, 0, 0)),
    b.blockAt(p.offset(0, 0, 1)),
    b.blockAt(p.offset(0, 0, -1))
  ].filter(isSolidGroundBlock).length;
  return eyeWalls === 4 && feetWalls >= 3;
}

function findNearestHostile(b, maxDist = 13) {
  if (!b?.entity) return null;
  const pos = b.entity.position;
  const recentlyHurt = Date.now() - lastHurtAt < 4000;
  let best = null;
  let bestD = Infinity;
  for (const e of Object.values(b.entities)) {
    if (!e || e === b.entity || !e.name || !e.position) continue;
    const n = e.name.toLowerCase();
    const isHostile = HOSTILE_MOBS.has(n) || (n === 'enderman' && recentlyHurt);
    if (!isHostile) continue;
    const d = e.position.distanceTo(pos);
    if (d < maxDist && d < bestD) {
      bestD = d;
      best = e;
    }
  }
  return best;
}

function pickBestEscapePoint(b, pos, threats) {
  const list = (Array.isArray(threats) ? threats : [threats])
    .map((t) => ({ p: t?.position || t, w: t?.name?.toLowerCase() === 'creeper' ? 2.2 : 1 }))
    .filter((t) => t.p && t.p.distanceTo(pos) < 16);
  let rx = 0;
  let rz = 0;
  for (const t of list) {
    const dx = pos.x - t.p.x;
    const dz = pos.z - t.p.z;
    const d = Math.hypot(dx, dz) || 1;
    const weight = t.w / Math.max(1, d);
    rx += (dx / d) * weight;
    rz += (dz / d) * weight;
  }
  const rlen = Math.hypot(rx, rz) || 1;
  rx /= rlen;
  rz /= rlen;

  const hut = loadMemory().hutLocation;
  let hx = 0;
  let hz = 0;
  if (hut) {
    const dxHome = hut.x - pos.x;
    const dzHome = hut.z - pos.z;
    const dHome = Math.hypot(dxHome, dzHome);
    if (dHome > 24) {
      hx = dxHome / dHome;
      hz = dzHome / dHome;
    }
  }

  let best = null;
  let bestScore = -Infinity;
  for (let i = 0; i < 8; i++) {
    const angle = (i * Math.PI) / 4;
    const ux = Math.cos(angle);
    const uz = Math.sin(angle);
    let score = (ux * rx + uz * rz) * 14 + (ux * hx + uz * hz) * 6;
    for (const dist of [2, 4]) {
      const sp = pos.floored().offset(Math.round(ux * dist), 0, Math.round(uz * dist));
      const feet = b.blockAt(sp);
      const head = b.blockAt(sp.offset(0, 1, 0));
      const up2 = b.blockAt(sp.offset(0, 2, 0));
      const d1 = b.blockAt(sp.offset(0, -1, 0));
      const d2 = b.blockAt(sp.offset(0, -2, 0));
      if (WATER_NAMES.has(feet?.name) || LAVA_NAMES.has(feet?.name) || feet?.name === 'powder_snow') score -= 25;
      if (WATER_NAMES.has(d1?.name) || LAVA_NAMES.has(d1?.name)) score -= 20;
      const clear = isPassableBlock(feet) && isPassableBlock(head);
      const stepUp = isPassableBlock(head) && isPassableBlock(up2);
      score += clear ? 8 : (stepUp ? 3 : -14);
      score += (isSolidGroundBlock(d1) || isSolidGroundBlock(d2)) ? 5 : -14;
    }
    if (score > bestScore) {
      bestScore = score;
      best = new Vec3(Math.round(pos.x + ux * 20), pos.y, Math.round(pos.z + uz * 20));
    }
  }
  return best || new Vec3(Math.round(pos.x + rx * 20), pos.y, Math.round(pos.z + rz * 20));
}

function hasRoofAbove(b, maxHeight = 6) {
  if (!b?.entity) return false;
  const p = b.entity.position.floored();
  for (let dy = 2; dy <= maxHeight; dy++) {
    const blk = b.blockAt(p.offset(0, dy, 0));
    if (blk && isSolidGroundBlock(blk) && !blk.name.includes('leaves')) return true;
  }
  return false;
}

function isIndoorsAtHome(b) {
  if (!b?.entity) return false;
  const hut = loadMemory().hutLocation;
  if (!hut) return false;
  const dist = b.entity.position.distanceTo(new Vec3(hut.x, hut.y, hut.z));
  if (dist > 10 || !hasRoofAbove(b, 7)) return false;
  const p = b.entity.position.floored();
  let wallDirs = 0;
  for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
    for (let step = 1; step <= 7; step++) {
      const w = b.blockAt(p.offset(dx * step, 1, dz * step));
      if (w && isSolidGroundBlock(w)) {
        wallDirs++;
        break;
      }
    }
  }
  return wallDirs >= 3;
}

let lastSleepAttemptAt = 0;
let sleepingSince = 0;
let lastReflexEatAt = 0;

function stopCombat(b) {
  try {
    if (b?.pvp?.target) b.pvp.stop();
  } catch (e) {}
  pvpStartedAt = 0;
}

async function equipBestWeapon(b) {
  for (const name of ['diamond_sword', 'iron_sword', 'stone_sword', 'wooden_sword', 'diamond_axe', 'iron_axe', 'stone_axe', 'wooden_axe', 'pickaxe']) {
    const it = await equipItemByName(b, name);
    if (it) return it;
  }
  return null;
}

async function equipBestGear(b) {
  if (!b?.inventory) return [];
  const equipped = [];
  const w = await equipBestWeapon(b);
  if (w) equipped.push(w.name);
  const shield = b.inventory.items().find((i) => i.name === 'shield');
  if (shield) {
    try { await b.equip(shield, 'off-hand'); equipped.push('shield'); } catch (e) {}
  }
  const slotTypes = [
    ['head', ['netherite_helmet', 'diamond_helmet', 'iron_helmet', 'chainmail_helmet', 'golden_helmet', 'leather_helmet']],
    ['torso', ['netherite_chestplate', 'diamond_chestplate', 'iron_chestplate', 'chainmail_chestplate', 'golden_chestplate', 'leather_chestplate']],
    ['legs', ['netherite_leggings', 'diamond_leggings', 'iron_leggings', 'chainmail_leggings', 'golden_leggings', 'leather_leggings']],
    ['feet', ['netherite_boots', 'diamond_boots', 'iron_boots', 'chainmail_boots', 'golden_boots', 'leather_boots']]
  ];
  for (const [slot, names] of slotTypes) {
    for (const n of names) {
      const armor = b.inventory.items().find((i) => i.name === n);
      if (armor) {
        try { await b.equip(armor, slot); equipped.push(n); } catch (e) {}
        break;
      }
    }
  }
  return equipped;
}

async function sprintEscape(b, threatsOrPos, primaryThreatPos, reasonLabel) {
  if (!b?.entity || !b.pathfinder || !mcData) return false;
  const now = Date.now();
  const pos = b.entity.position;
  lastThreatEscapeAt = now;
  escapeActive = true;

  if (now - lastEscapeLogAt > 3500) {
    lastEscapeLogAt = now;
    log(`${reasonLabel} (HP ${Math.round(b.health || 20)}) – sprinte in Sicherheit.`);
  }

  // Keep continuous pathfinder sprint running instead of resetting/stopping every 850ms
  const urgent = primaryThreatPos && pos.distanceTo(primaryThreatPos) < 4.2;
  if (b.pathfinder.isMoving() && now - lastEscapeGoalAt < 1200 && !urgent) {
    b.setControlState('sprint', true);
    return true;
  }

  stopCombat(b);
  const escapePt = pickBestEscapePoint(b, pos, threatsOrPos);
  const hut = loadMemory().hutLocation;
  const hutVec = hut ? new Vec3(hut.x, hut.y, hut.z) : null;
  let targetPt = escapePt;
  if (hutVec && pos.distanceTo(hutVec) <= 35) {
    const toHome = hutVec.minus(pos);
    const toMob = primaryThreatPos ? primaryThreatPos.minus(pos) : new Vec3(0, 0, 0);
    const dot = toHome.x * toMob.x + toHome.z * toMob.z;
    if (pos.distanceTo(hutVec) <= 10 || dot <= 0) {
      targetPt = hutVec;
    }
  }

  lastEscapeGoalAt = now;
  try {
    b.pathfinder.setMovements(buildSafeMovements(b, true));
    b.pathfinder.setGoal(new goals.GoalXZ(targetPt.x, targetPt.z));
    b.setControlState('sprint', true);
  } catch (e) {}
  return true;
}

async function runSurvivalReflexes(b) {
  if (!b?.entity || !mcData || pvpBusy || shelterBusy) return;
  pvpBusy = true;
  try {
    // 1) Reactive eat whenever food/HP drops
    if ((b.food <= 16 || (b.health < 17 && b.food < 20)) && Date.now() - lastReflexEatAt > 2500) {
      lastReflexEatAt = Date.now();
      await autoEatIfHungry(b);
    }

    const pos = b.entity.position;
    const recentlyHurt = Date.now() - lastHurtAt < 4000;
    const hostiles = Object.values(b.entities).filter((e) => {
      if (!e || e === b.entity || !e.name || !e.position) return false;
      const n = e.name.toLowerCase();
      return HOSTILE_MOBS.has(n) || (n === 'enderman' && recentlyHurt);
    });

    // 2) Mob-friendly pacifist mode: never attack any mob; only step aside from imminent Creeper blast or direct hits
    stopCombat(b);
    const nearCreeper = hostiles
      .filter((e) => e.name.toLowerCase() === 'creeper' && e.position.distanceTo(pos) < 5.5)
      .sort((a, b2) => a.position.distanceTo(pos) - b2.position.distanceTo(pos))[0];
    if (nearCreeper) {
      await sprintEscape(b, hostiles, nearCreeper.position, `Mache Platz für Creeper (${nearCreeper.position.distanceTo(pos).toFixed(1)}m)`);
      return;
    }

    const closeAttackers = (recentlyHurt || b.health < 12)
      ? hostiles
          .filter((e) => e.name.toLowerCase() !== 'creeper' && e.position.distanceTo(pos) < (recentlyHurt ? 4.5 : 3.0))
          .sort((a, b2) => a.position.distanceTo(pos) - b2.position.distanceTo(pos))
      : [];

    if (closeAttackers.length === 0) {
      if (escapeActive) {
        escapeActive = false;
        try {
          b.setControlState('sprint', false);
          b.pathfinder?.setGoal(null);
        } catch (e) {}
      }
      return;
    }

    const threat = closeAttackers[0];
    await sprintEscape(b, hostiles, threat.position, `Weiche friedlich aus (${threat.name})`);
    return;
  } finally {
    pvpBusy = false;
  }
}

async function handleSurvivalThreat(b) {
  if (!b?.entity) return false;
  if (b.pvp?.target) return true;
  await runSurvivalReflexes(b);
  return Boolean(b.pvp?.target);
}

const SHELTER_BLOCKS = ['dirt', 'cobblestone', 'cobbled_deepslate', 'spruce_log', 'oak_log', 'birch_log', 'gravel', 'sand', 'snow_block'];

function findShelterMaterial(b) {
  for (const name of SHELTER_BLOCKS) {
    const found = b.inventory.items().find((it) => it.name === name);
    if (found) return found;
  }
  return null;
}

async function placeShelterBlockDirect(b, targetPos) {
  const cur = b.blockAt(targetPos);
  if (!cur) return false;
  if (isSolidGroundBlock(cur)) return true;
  if (!isPassableBlock(cur)) {
    if (!SAFE_NATURAL_BLOCKS.some((k) => cur.name.includes(k)) || WATER_NAMES.has(cur.name) || LAVA_NAMES.has(cur.name)) {
      return false;
    }
    await withTimeout(b.dig(cur), 1800);
    if (!isPassableBlock(b.blockAt(targetPos))) return false;
  }
  const item = findShelterMaterial(b);
  if (!item) return false;
  try { await b.equip(item, 'hand'); } catch (e) { return false; }

  const faces = [
    new Vec3(0, -1, 0),
    new Vec3(1, 0, 0),
    new Vec3(-1, 0, 0),
    new Vec3(0, 0, 1),
    new Vec3(0, 0, -1),
    new Vec3(0, 1, 0)
  ];
  for (const f of faces) {
    const ref = b.blockAt(targetPos.plus(f));
    if (ref && isSolidGroundBlock(ref)) {
      try {
        await withTimeout(b.placeBlock(ref, f.scaled(-1)), 1500);
      } catch (e) {}
      if (isSolidGroundBlock(b.blockAt(targetPos))) return true;
    }
  }
  return false;
}

async function sealFoxhole(b, base) {
  // Seal any open eye-level wall (dy=1) and feet-level wall (dy=0) without moving out of the hole.
  for (const dy of [0, 1]) {
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const sidePos = base.offset(dx, dy, dz);
      if (!isSolidGroundBlock(b.blockAt(sidePos))) {
        await placeShelterBlockDirect(b, sidePos);
      }
    }
  }
  const roofPos = base.offset(0, 2, 0);
  if (!isSolidGroundBlock(b.blockAt(roofPos))) {
    let placed = await placeShelterBlockDirect(b, roofPos);
    if (!placed) {
      // Place a temporary/side anchor block on top of a solid eye-level wall so roofPos has a horizontal face.
      for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        if (isSolidGroundBlock(b.blockAt(base.offset(dx, 1, dz)))) {
          if (await placeShelterBlockDirect(b, base.offset(dx, 2, dz))) {
            placed = await placeShelterBlockDirect(b, roofPos);
            if (placed) break;
          }
        }
      }
    }
  }
  return isShelteredInPlace(b);
}

async function ensureNightShelter(b) {
  if (!b?.entity || shelterBusy) return false;
  shelterBusy = true;
  try {
    const mem = loadMemory();
    const pos = b.entity.position.floored();
    const night = isNightTime(b);

    if (!night) {
      if (mem.nightShelterActive) {
        log('Es wird hell – verlasse meinen Nacht-Unterschlupf und packe wieder an.');
        for (const dy of [2, 3, 4]) {
          const above = b.blockAt(b.entity.position.floored().offset(0, dy, 0));
          if (above && !isPassableBlock(above) && SAFE_NATURAL_BLOCKS.some((k) => above.name.includes(k))) {
            await withTimeout(b.dig(above), 3500);
          }
        }
        for (let i = 0; i < 4; i++) {
          const cur = b.entity.position.floored();
          const exits = [cur.offset(1, 1, 0), cur.offset(-1, 1, 0), cur.offset(0, 1, 1), cur.offset(0, 1, -1)];
          if (exits.some((p) => isPassableBlock(b.blockAt(p)))) break;
          const fill = findShelterMaterial(b);
          if (!fill) { await tryUnstuck(b); break; }
          try { await b.equip(fill, 'hand'); } catch (e) { break; }
          const below = b.blockAt(cur.offset(0, -1, 0));
          if (!below) break;
          b.setControlState('jump', true);
          await new Promise((r) => setTimeout(r, 240));
          try { await withTimeout(b.placeBlock(below, new Vec3(0, 1, 0)), 1500); } catch (e) {}
          b.setControlState('jump', false);
          await new Promise((r) => setTimeout(r, 150));
        }
        mem.nightShelterActive = false;
        saveMemory(mem);
      }
      return false;
    }

    // Nighttime: 1) Only treat as "at home" when actually near home (<=28m), NOT at world spawn (112m away)!
    const home = mem.hutLocation ? new Vec3(mem.hutLocation.x, mem.hutLocation.y, mem.hutLocation.z) : null;
    if (home && pos.distanceTo(home) <= 28) {
      if (mem.nightShelterActive) {
        mem.nightShelterActive = false;
        saveMemory(mem);
      }
      if (!b.isSleeping && Date.now() - lastSleepAttemptAt > 60000) {
        lastSleepAttemptAt = Date.now();
        const closeThreat = findNearestHostile(b, 6);
        const bedBlock = !closeThreat && b.findBlock({ matching: (blk) => blk && blk.name && blk.name.endsWith('_bed'), maxDistance: 16 });
        if (bedBlock) {
          try {
            await moveNear(b, bedBlock.position, 2, 5000);
            await withTimeout(b.sleep(bedBlock), 2500);
            if (b.isSleeping) {
              sleepingSince = Date.now();
              log('Schlafe ruhig in meinem Bett bis zum Morgen.');
              return true;
            }
          } catch (e) {}
        }
      }
      if (b.isSleeping) {
        if (sleepingSince && Date.now() - sleepingSince > 15000) {
          sleepingSince = 0;
          await withTimeout(b.wake(), 1500).catch(() => {});
          return false;
        }
        return true;
      }
      sleepingSince = 0;
      return false;
    }

    // 2) Far from home (>28m) at night: dig quick emergency foxhole when low HP or unarmed and no mob within 8m
    if (isShelteredInPlace(b)) {
      b.clearControlStates();
      if (!mem.nightShelterActive) {
        mem.nightShelterActive = true;
        saveMemory(mem);
      }
      log(`Nacht-Schutz aktiv (HP ${Math.round(b.health || 20)}/20) – warte sicher auf den Sonnenaufgang.`);
      return true;
    }
    const hasWeapon = b.inventory.items().some((i) => i.name.includes('_sword') || i.name.includes('_axe'));
    if ((b.health > 13 && hasWeapon) || findNearestHostile(b, 8)) return false;

    const canDig = (blk) => blk && SAFE_NATURAL_BLOCKS.some((k) => blk.name.includes(k)) && !WATER_NAMES.has(blk.name) && !LAVA_NAMES.has(blk.name);
    const yieldsBlock = (blk) => blk && (SOFT_SOIL_BLOCKS.has(blk.name) || blk.name.includes('_log'));
    const isGoodDigColumn = (p) => {
      const f = b.blockAt(p);
      const d1 = b.blockAt(p.offset(0, -1, 0));
      const d2 = b.blockAt(p.offset(0, -2, 0));
      const d3 = b.blockAt(p.offset(0, -3, 0));
      if (!f || WATER_NAMES.has(f.name) || LAVA_NAMES.has(f.name)) return false;
      if (!isPassableBlock(f) && !canDig(f)) return false;
      if (!canDig(d1) || !canDig(d2) || !isSolidGroundBlock(d3) || hasLavaNearby(b, p.offset(0, -1, 0))) return false;
      return Boolean(findShelterMaterial(b) || yieldsBlock(d1) || yieldsBlock(d2));
    };

    // If current spot isn't suitable for digging a foxhole (e.g. stone/leaves/water/empty inv), find/move to a nearby soil column or gather 2 dirt blocks
    let digOrigin = b.entity.position.floored();
    if (!isGoodDigColumn(digOrigin)) {
      if (!findShelterMaterial(b)) {
        const soilPosList = b.findBlocks({ matching: (blk) => blk && SOFT_SOIL_BLOCKS.has(blk.name), maxDistance: 6, count: 6 });
        for (const sp of soilPosList) {
          const sb = b.blockAt(sp);
          if (!sb || b.entity.position.distanceTo(sp) > 4.2) continue;
          await withTimeout(b.dig(sb), 2500);
          await new Promise((r) => setTimeout(r, 300));
          await collectNearbyDrops(b);
          if (findShelterMaterial(b)) break;
        }
      }
      if (!isGoodDigColumn(b.entity.position.floored())) {
        let bestSpot = null;
        let bestDist = Infinity;
        const p0 = b.entity.position.floored();
        for (let dx = -5; dx <= 5; dx++) {
          for (let dz = -5; dz <= 5; dz++) {
            for (let dy = -2; dy <= 2; dy++) {
              const cand = p0.offset(dx, dy, dz);
              if (isGoodDigColumn(cand)) {
                const dist = Math.hypot(dx, dz) + Math.abs(dy);
                if (dist < bestDist) {
                  bestDist = dist;
                  bestSpot = cand;
                }
              }
            }
          }
        }
        if (bestSpot) {
          await moveNear(b, bestSpot, 1, 5000);
        }
      }
    }

    const cp0 = b.entity.position.floored();
    const curBlk = b.blockAt(cp0);
    if (curBlk && !isPassableBlock(curBlk) && canDig(curBlk)) {
      await withTimeout(b.dig(curBlk), 2000);
    }

    if (!b.entity.isInWater && isGoodDigColumn(b.entity.position.floored())) {
      log(`Es ist Nacht bei ${b.entity.position.floored()} – grabe mir einen sicheren Not-Unterschlupf.`);
      b.clearControlStates();
      for (let step = 0; step < 3; step++) {
        const cp = b.entity.position.floored();
        const below = b.blockAt(cp.offset(0, -1, 0));
        const support = b.blockAt(cp.offset(0, -2, 0));
        if (!canDig(below) || !isSolidGroundBlock(support) || hasLavaNearby(b, cp.offset(0, -1, 0))) break;
        await withTimeout(b.dig(below), 3500);
        await new Promise((r) => setTimeout(r, 350));
      }
      const base = b.entity.position.floored();
      const sealed = await sealFoxhole(b, base);
      if (sealed) {
        mem.nightShelterActive = true;
        saveMemory(mem);
        log(`Not-Unterschlupf bei ${base} komplett verschlossen (HP ${Math.round(b.health || 20)}/20).`);
        return true;
      }
    }

    return false;
  } finally {
    shelterBusy = false;
  }
}

function countInv(b, predicate) {
  return b.inventory.items().filter(predicate).reduce((sum, i) => sum + i.count, 0);
}

async function craftSurvivalItem(b, itemName, count = 1) {
  if (!mcData) return false;
  const itemDef = mcData.itemsByName?.[itemName];
  if (!itemDef) return false;

  let tableBlock = b.findBlock({ matching: (blk) => blk && blk.name === 'crafting_table', maxDistance: 8 });
  let recipes = b.recipesFor(itemDef.id, null, 1, tableBlock || null);
  let placedTempTable = null;

  if ((!recipes || recipes.length === 0) && !tableBlock) {
    let ctInInv = b.inventory.items().find((i) => i.name === 'crafting_table');
    if (!ctInInv && itemName !== 'crafting_table') {
      await craftSurvivalItem(b, 'crafting_table', 1);
      ctInInv = b.inventory.items().find((i) => i.name === 'crafting_table');
    }
    if (ctInInv) {
      const groundCandidates = b.findBlocks({
        matching: (blk) => Boolean(blk && isSolidGroundBlock(blk)),
        maxDistance: 5,
        count: 30
      });
      const groundPos =
        groundCandidates.find((gp) => !isInsideProtectedHome(gp, 6) && isPassableBlock(b.blockAt(gp.offset(0, 1, 0)))) ||
        groundCandidates.find((gp) => isPassableBlock(b.blockAt(gp.offset(0, 1, 0))));
      if (groundPos) {
        const placePos = groundPos.offset(0, 1, 0);
        if (await placeBlockAt(b, placePos, 'crafting_table')) {
          placedTempTable = placePos;
          tableBlock = b.findBlock({ matching: (blk) => blk && blk.name === 'crafting_table', maxDistance: 6 });
        }
      }
    }
    recipes = b.recipesFor(itemDef.id, null, 1, tableBlock || null);
  }

  if (!recipes || recipes.length === 0) return false;
  if (tableBlock) await moveNear(b, tableBlock.position, 3, 6000);
  const beforeCount = countInv(b, (i) => i.name === itemName);
  try {
    const res = await withTimeout(b.craft(recipes[0], count, tableBlock || null), 4000);
    if (res && (res._err || res._timeout)) return false;
    const afterCount = countInv(b, (i) => i.name === itemName);
    if (afterCount > beforeCount) {
      log(`Selbst gecraftet: ${afterCount - beforeCount}x ${itemName}`);
      return true;
    }
    return false;
  } catch (e) {
    return false;
  } finally {
    // Clean up temporary crafting table placed out in the woods so Rudi keeps it in his bag.
    if (placedTempTable && !isInsideProtectedHome(placedTempTable, 12)) {
      const tb = b.blockAt(placedTempTable);
      if (tb && tb.name === 'crafting_table') {
        await withTimeout(b.dig(tb), 3000);
        await collectNearbyDrops(b);
      }
    }
  }
}

async function craftWithChain(b, itemName, count = 1) {
  if (!b?.entity || !mcData) return false;
  if (await craftSurvivalItem(b, itemName, count)) return true;
  // Auto-prepare planks & sticks from raw logs if needed
  const rawLog = b.inventory.items().find((i) => i.name.endsWith('_log'));
  if (rawLog) {
    const plankName = rawLog.name.replace(/_log$/, '_planks');
    await craftSurvivalItem(b, plankName, Math.min(rawLog.count, 3));
  }
  if (countInv(b, (i) => i.name === 'stick') < 4 && countInv(b, (i) => i.name.endsWith('_planks')) >= 2) {
    await craftSurvivalItem(b, 'stick', 1);
  }
  return craftSurvivalItem(b, itemName, count);
}

async function ensureSurvivalGear(b) {
  if (!b?.entity || !mcData) return false;
  let craftedAny = false;

  // Convert raw logs to planks first when planks are low
  const rawLog = b.inventory.items().find((i) => i.name.endsWith('_log'));
  const planksTotal = countInv(b, (i) => i.name.endsWith('_planks'));
  if (rawLog && planksTotal < 16) {
    const plankName = rawLog.name.replace(/_log$/, '_planks');
    if (await craftSurvivalItem(b, plankName, Math.min(rawLog.count, 3))) craftedAny = true;
  }

  const planksNow = countInv(b, (i) => i.name.endsWith('_planks'));
  if (countInv(b, (i) => i.name === 'stick') < 4 && planksNow >= 2) {
    if (await craftSurvivalItem(b, 'stick', 1)) craftedAny = true;
  }

  const hasStone = countInv(b, (i) => i.name === 'cobblestone' || i.name === 'cobbled_deepslate') >= 3;
  const hasSword = b.inventory.items().some((i) => i.name.includes('_sword'));
  const hasAxe = b.inventory.items().some((i) => i.name.includes('_axe'));
  const hasPick = b.inventory.items().some((i) => i.name.includes('_pickaxe'));

  if (!hasSword && (hasStone || planksNow >= 2)) {
    const targetSword = hasStone ? 'stone_sword' : 'wooden_sword';
    if (await craftSurvivalItem(b, targetSword, 1)) craftedAny = true;
  }
  if (!hasAxe && (hasStone || planksNow >= 3)) {
    const targetAxe = hasStone ? 'stone_axe' : 'wooden_axe';
    if (await craftSurvivalItem(b, targetAxe, 1)) craftedAny = true;
  }
  if (!hasPick && (hasStone || planksNow >= 3)) {
    const targetPick = hasStone ? 'stone_pickaxe' : 'wooden_pickaxe';
    if (await craftSurvivalItem(b, targetPick, 1)) craftedAny = true;
  }
  if (countInv(b, (i) => i.name === 'coal' || i.name === 'charcoal') >= 1 && countInv(b, (i) => i.name === 'stick') >= 1 && countInv(b, (i) => i.name === 'torch') < 8) {
    if (await craftSurvivalItem(b, 'torch', 2)) craftedAny = true;
  }
  return craftedAny;
}

async function collectNearbyDrops(b) {
  const drop = b.nearestEntity((e) => e && e.name === 'item' && e.position.distanceTo(b.entity.position) < 8);
  if (drop) {
    await moveToGoal(b, new goals.GoalXZ(drop.position.x, drop.position.z), 4000);
  }
}

async function clearBlockIfReplaceable(b, pos) {
  const cur = b.blockAt(pos);
  if (!cur) return false;
  if (cur.name === 'air' || cur.name === 'cave_air') return true;
  if (['snow', 'powder_snow', 'short_grass', 'tall_grass', 'fern', 'spruce_leaves'].includes(cur.name)) {
    await withTimeout(b.dig(cur), 2000);
    return true;
  }
  return false;
}

async function placeBlockAt(b, targetPos, itemQuery) {
  const cur = b.blockAt(targetPos);
  if (!cur) return false;
  if (cur.name !== 'air' && cur.name !== 'cave_air') {
    const cleared = await clearBlockIfReplaceable(b, targetPos);
    if (!cleared) return false;
  }
  const item = await equipItemByName(b, itemQuery);
  if (!item) return false;

  const faces = [
    new Vec3(0, -1, 0),
    new Vec3(1, 0, 0),
    new Vec3(-1, 0, 0),
    new Vec3(0, 0, 1),
    new Vec3(0, 0, -1)
  ];
  for (const f of faces) {
    const ref = b.blockAt(targetPos.plus(f));
    if (ref && ref.name !== 'air' && ref.name !== 'cave_air' && ref.name !== 'snow' && ref.name !== 'water') {
      if (b.entity.position.distanceTo(targetPos) < 1.2) {
        b.setControlState('jump', true);
        await new Promise((r) => setTimeout(r, 150));
        b.setControlState('jump', false);
      }
      await moveNear(b, targetPos, 3, 5000);
      await withTimeout(b.placeBlock(ref, f.scaled(-1)), 2500);
      const after = b.blockAt(targetPos);
      if (after && after.name !== 'air' && after.name !== 'cave_air') {
        return true;
      }
    }
  }
  return false;
}

async function buildOrMaintainHut(b) {
  const mem = loadMemory();
  if (!mem.hutLocation) {
    mem.hutLocation = { x: -6070, y: 81, z: -3223 };
    saveMemory(mem);
  }
  const h = mem.hutLocation;
  const homeVec = new Vec3(h.x, h.y, h.z);
  log(`Laufe nach Hause zu meiner Försterhütte bei (${h.x}, ${h.y}, ${h.z})...`);
  const beforePos = b.entity.position.clone();
  await moveNear(b, homeVec, 3, 25000);
  if (b.entity.position.distanceTo(homeVec) > 12 && !b.pvp?.target && !escapeActive) {
    const moved = b.entity.position.distanceTo(beforePos);
    if (b.entity.position.y < h.y - 3 || moved < 5) {
      await digStaircaseToSurface(b, homeVec, 14);
      await moveNear(b, homeVec, 3, 20000);
    }
  }
  if (mem.hutBuilt || mem.homeGifted) {
    if (b.inventory.items().some((i) => i.name.includes('_log')) && !b.inventory.items().some((i) => i.name.includes('wooden_axe'))) {
      await craftSurvivalItem(b, 'spruce_planks', 2);
      await craftSurvivalItem(b, 'stick', 1);
      await craftSurvivalItem(b, 'wooden_axe', 1);
    }
    b.swingArm();
    return;
  }
  if (b.entity.position.distanceTo(new Vec3(h.x, h.y, h.z)) > 8) {
    log('Bin noch auf dem Rückweg zu meiner Hütte und mache beim nächsten Schritt weiter.');
    return;
  }

  // Gather wood first if completely out of planks and logs
  if (!b.inventory.items().some((i) => i.name.includes('spruce_planks') || i.name.includes('_log'))) {
    await executeDecision(b, { actionType: 'chop_wood', thought: 'Hole mir erst Bauholz für die Hütte' });
    await craftSurvivalItem(b, 'spruce_planks', 4);
  }

  // Small 5x5 spruce cabin blueprint: floor (-1), walls (0..2), roof (3), interior furniture
  const blueprint = [];
  for (let dx = -2; dx <= 2; dx++) {
    for (let dz = -2; dz <= 2; dz++) {
      blueprint.push({ dx, dy: -1, dz, item: 'spruce_planks' });
      const isEdge = Math.abs(dx) === 2 || Math.abs(dz) === 2;
      const isCorner = Math.abs(dx) === 2 && Math.abs(dz) === 2;
      const isDoor = dx === 0 && dz === 2;
      if (isEdge && !isDoor) {
        for (let dy = 0; dy <= 2; dy++) {
          if (dy === 1 && !isCorner && (dx === 0 || dz === 0)) continue;
          blueprint.push({ dx, dy, dz, item: isCorner ? 'spruce_log' : 'spruce_planks' });
        }
      }
      blueprint.push({ dx, dy: 3, dz, item: 'spruce_planks' });
    }
  }
  // Cozy interior essentials
  blueprint.push({ dx: -1, dy: 0, dz: -1, item: 'red_bed' });
  blueprint.push({ dx: 1, dy: 0, dz: -1, item: 'crafting_table' });
  blueprint.push({ dx: 1, dy: 0, dz: 0, item: 'furnace' });
  blueprint.push({ dx: -1, dy: 0, dz: 0, item: 'chest' });
  blueprint.push({ dx: 0, dy: 2, dz: 0, item: 'lantern' });

  let placedCount = 0;
  let missingCount = 0;
  for (const step of blueprint) {
    if (placedCount >= 16) break;
    const targetPos = new Vec3(h.x + step.dx, h.y + step.dy, h.z + step.dz);
    const current = b.blockAt(targetPos);
    if (!current) continue;
    if (current.name !== 'air' && current.name !== 'cave_air' && current.name !== 'snow' && current.name !== 'powder_snow') {
      continue;
    }
    missingCount++;
    let hasItem = b.inventory.items().some((i) => i.name.includes(step.item));
    if (!hasItem && step.item === 'spruce_planks') {
      await craftSurvivalItem(b, 'spruce_planks', 2);
      hasItem = b.inventory.items().some((i) => i.name.includes(step.item));
    }
    if (!hasItem && ['crafting_table', 'chest', 'furnace'].includes(step.item)) {
      await craftSurvivalItem(b, step.item, 1);
      hasItem = b.inventory.items().some((i) => i.name.includes(step.item));
    }
    if (!hasItem) continue;
    const ok = await placeBlockAt(b, targetPos, step.item);
    if (ok) placedCount++;
  }

  log(`Hüttenbau-Fortschritt: ${placedCount} Blöcke gesetzt (offene Bau-Slots: ${missingCount}).`);
  if (missingCount === 0) {
    mem.hutBuilt = true;
    saveMemory(mem);
  }
  b.swingArm();
}

async function runCustomSkillCode(b, code, skillName, skillDescription) {
  if (!code || typeof code !== 'string') {
    return { ok: false, summary: 'Kein ausführbarer Skill-Code vorhanden' };
  }
  log(`Führe Skill-Code${skillName ? ` '${skillName}'` : ''} aus...`);
  try {
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    const fn = new AsyncFunction('bot', 'ctx', code);
    const ctx = {
      Vec3,
      goals,
      Movements,
      mcData,
      skills: {
        moveNear: (pos, range = 2, ms = 12000) => moveNear(b, pos, range, ms),
        digBlock: (blk) => withTimeout(b.dig(blk), 4500),
        placeBlock: (pos, itemName) => placeBlockAt(b, pos, itemName),
        collectDrops: () => collectNearbyDrops(b),
        craft: (itemName, count = 1) => craftWithChain(b, itemName, count),
        equip: (name, dest = 'hand') => equipItemByName(b, name, dest),
        log
      }
    };
    await Promise.race([
      fn(b, ctx),
      new Promise((_, rej) => setTimeout(() => rej(new Error('Skill code timeout')), 15000))
    ]);
    const learnedSkill = skillName ? { name: skillName, description: skillDescription || '', code } : null;
    return { ok: true, summary: `Skill '${skillName || 'custom_code'}' erfolgreich ausgeführt`, learnedSkill };
  } catch (err) {
    log(`Live-Code Hinweis: ${err.message}`);
    return { ok: false, summary: `Skill-Fehler: ${err.message}` };
  }
}

async function executeDecision(b, decision) {
  if (!b || !b.entity || !decision) return { ok: false, summary: 'Bot nicht bereit' };
  const action = decision.actionType;
  const target = (decision.target || '').trim();
  const hobbyLabel = decision.hobby ? `[${decision.hobby}] ` : '';

  log(`Gedanke ${hobbyLabel}: "${decision.thought || ''}" -> Aktion: ${action} (${target || '-'})`);

  if (decision.chat && Date.now() - lastChatReplyAt > 4000) {
    lastChatReplyAt = Date.now();
    b.chat(decision.chat.slice(0, 220));
  }

  switch (action) {
    case 'explore': {
      const p0 = b.entity.position.clone();
      const hut = loadMemory().hutLocation;
      const dirMap = { north: [0, -1], south: [0, 1], east: [1, 0], west: [-1, 0] };
      const vec = dirMap[target.toLowerCase()];

      let tx, tz;
      const wildTrees = b.findBlocks({
        matching: (blk) => blk?.name?.endsWith('_log') && !blk.name.startsWith('stripped_'),
        maxDistance: 112,
        count: 30
      }).filter((p) => !isHutLog(p, hut) && !unreachableLogs.has(logKey(p)));

      if (!vec && wildTrees.length > 0) {
        wildTrees.sort((a, b2) => a.distanceTo(p0) - b2.distanceTo(p0));
        tx = wildTrees[0].x;
        tz = wildTrees[0].z;
      } else {
        let angle;
        if (vec) {
          angle = Math.atan2(vec[1], vec[0]) + (Math.random() - 0.5) * 0.45;
        } else if (hut && p0.distanceTo(new Vec3(hut.x, hut.y, hut.z)) < 140) {
          angle = Math.atan2(p0.z - hut.z, p0.x - hut.x) + (Math.random() - 0.5) * 0.9;
        } else {
          angle = Math.random() * Math.PI * 2;
        }
        const dist = 48 + Math.floor(Math.random() * 18);
        tx = Math.round(p0.x + Math.cos(angle) * dist);
        tz = Math.round(p0.z + Math.sin(angle) * dist);
      }

      await moveToGoal(b, new goals.GoalXZ(tx, tz), 20000, false);
      const moved = Math.round(b.entity.position.distanceTo(p0));
      const treesNearby = b.findBlocks({
        matching: (blk) => blk?.name?.endsWith('_log') && !blk.name.startsWith('stripped_'),
        maxDistance: 64,
        count: 8
      }).filter((p) => !isHutLog(p, hut) && !unreachableLogs.has(logKey(p))).length;
      return {
        ok: moved >= 8,
        summary: moved >= 8
          ? `${moved}m Richtung (${tx}, ${tz}) erkundet (wilde Bäume dort: ${treesNearby})`
          : `Erkundung Richtung (${tx}, ${tz}) blockiert (nur ${moved}m bewegt)`
      };
    }

    case 'wander': {
      const mem = loadMemory();
      const p0 = b.entity.position.clone();
      if (isNightTime(b) && isIndoorsAtHome(b)) {
        b.swingArm();
        return { ok: true, summary: 'Nachts sicher in der Hütte geblieben' };
      }
      let tx, tz;
      if (isNightTime(b) && mem.hutLocation) {
        tx = Math.round(mem.hutLocation.x + (Math.random() - 0.5) * 8);
        tz = Math.round(mem.hutLocation.z + (Math.random() - 0.5) * 8);
      } else if (mem.hutLocation && p0.distanceTo(new Vec3(mem.hutLocation.x, mem.hutLocation.y, mem.hutLocation.z)) > 35) {
        tx = Math.round(mem.hutLocation.x + (Math.random() - 0.5) * 16);
        tz = Math.round(mem.hutLocation.z + (Math.random() - 0.5) * 16);
      } else {
        const angle = Math.random() * Math.PI * 2;
        const dist = 10 + Math.floor(Math.random() * 16);
        tx = Math.round(p0.x + Math.cos(angle) * dist);
        tz = Math.round(p0.z + Math.sin(angle) * dist);
      }
      await moveToGoal(b, new goals.GoalXZ(tx, tz), 15000, false);
      b.swingArm();
      const moved = Math.round(b.entity.position.distanceTo(p0));
      return { ok: moved >= 3, summary: `Spaziergang nach (${tx}, ${tz}) – ${moved}m bewegt` };
    }

    case 'build_hut': {
      const hut = loadMemory().hutLocation;
      const startDist = hut ? Math.round(b.entity.position.distanceTo(new Vec3(hut.x, hut.y, hut.z))) : 0;
      await buildOrMaintainHut(b);
      const dist = hut ? Math.round(b.entity.position.distanceTo(new Vec3(hut.x, hut.y, hut.z))) : 0;
      const madeProgress = dist <= 12 || (startDist - dist) >= 18;
      return { ok: madeProgress, summary: `Bei Försterhütte (${dist}m Abstand)` };
    }

    case 'chop_wood': {
      const wantCount = Math.min(8, Math.max(1, parseInt(target, 10) || 4));
      const beforeLogs = countInv(b, (i) => i.name.endsWith('_log') || i.name.endsWith('_planks'));
      const now = Date.now();
      for (const [k, exp] of unreachableLogs) {
        if (exp <= now) unreachableLogs.delete(k);
      }
      const mem = loadMemory();
      const hut = mem.hutLocation;

      const myPos = b.entity.position;
      const candidates = b.findBlocks({
        matching: (blk) => blk && blk.name && blk.name.endsWith('_log') && !blk.name.startsWith('stripped_'),
        maxDistance: 80,
        count: 500
      })
        .filter((pos) => !isHutLog(pos, hut) && !unreachableLogs.has(logKey(pos)))
        .sort((a, b2) => ((Math.abs(a.y - myPos.y) > 4 ? 35 : 0) + a.distanceTo(myPos)) - ((Math.abs(b2.y - myPos.y) > 4 ? 35 : 0) + b2.distanceTo(myPos)));

      const moves = buildSafeMovements(b, false);
      let chopped = 0;
      let attempts = 0;
      for (const pos of candidates) {
        if (chopped >= wantCount || attempts >= 6 || b.pvp?.target || escapeActive) break;
        const pRes = b.pathfinder.getPathTo(moves, new goals.GoalNear(pos.x, pos.y, pos.z, 2));
        if (!pRes || (pRes.status !== 'success' && pRes.status !== 'partial') || !pRes.path?.length) {
          unreachableLogs.set(logKey(pos), Date.now() + UNREACHABLE_LOG_TTL_MS);
          continue;
        }
        attempts++;
        await equipItemByName(b, 'axe');
        await moveNear(b, pos, 2, 12000);
        if (b.pvp?.target || escapeActive) break;
        if (b.entity.position.distanceTo(pos) > 4.5) {
          unreachableLogs.set(logKey(pos), Date.now() + UNREACHABLE_LOG_TTL_MS);
          continue;
        }
        const blk = b.blockAt(pos);
        if (!blk || !blk.name.includes('_log')) continue;
        await withTimeout(b.dig(blk), 4500);
        chopped++;
        // Also harvest trunk blocks directly above if reachable without towering
        for (let dy = 1; dy <= 3 && chopped < wantCount; dy++) {
          const upBlk = b.blockAt(pos.offset(0, dy, 0));
          if (upBlk && upBlk.name.endsWith('_log') && b.entity.position.distanceTo(upBlk.position) <= 4.5) {
            await withTimeout(b.dig(upBlk), 4000);
            chopped++;
          } else {
            break;
          }
        }
        await new Promise((r) => setTimeout(r, 400));
        await collectNearbyDrops(b);
      }
      if (chopped === 0 && b.entity.position.y < 70 && !b.pvp?.target && !escapeActive) {
        await digStaircaseToSurface(b, hut ? new Vec3(hut.x, hut.y, hut.z) : null, 12);
      }
      const afterLogs = countInv(b, (i) => i.name.endsWith('_log') || i.name.endsWith('_planks'));
      const gained = Math.max(chopped, afterLogs - beforeLogs);
      if (chopped > 0) {
        return { ok: true, summary: `${chopped}x Baumstamm geschlagen (+${gained} Holz im Inventar)` };
      }
      return { ok: false, summary: 'Kein erreichbarer Baum innerhalb 80m gefunden (0 Holz geholt)' };
    }

    case 'craft': {
      const rawName = (target || '').replace(/^minecraft:/, '') || 'wooden_sword';
      const before = countInv(b, (i) => i.name === rawName);
      let ok = await craftWithChain(b, rawName, 1);
      if (!ok) {
        await ensureSurvivalGear(b);
        ok = countInv(b, (i) => i.name === rawName) > before;
      }
      await equipBestGear(b);
      return ok
        ? { ok: true, summary: `${rawName} erfolgreich gecraftet & ausgerüstet` }
        : { ok: false, summary: `${rawName} konnte nicht gecraftet werden (Material oder Werkbank fehlt)` };
    }

    case 'mine': {
      const wantOre = target.toLowerCase();
      const targetNames = wantOre.includes('coal')
        ? ['coal_ore', 'deepslate_coal_ore', 'stone', 'cobblestone']
        : (wantOre.includes('iron')
          ? ['iron_ore', 'deepslate_iron_ore', 'coal_ore', 'stone']
          : ['stone', 'cobblestone', 'coal_ore', 'iron_ore', 'copper_ore']);
      const moves = buildSafeMovements(b, false);
      const myPos = b.entity.position.floored();
      const candidates = b.findBlocks({
        matching: (blk) => blk && targetNames.includes(blk.name),
        maxDistance: 36,
        count: 60
      })
        .filter((pos) => {
          if (isInsideProtectedHome(pos, 12)) return false;
          if (pos.y < myPos.y && Math.abs(pos.x - myPos.x) <= 1 && Math.abs(pos.z - myPos.z) <= 1) return false;
          if (pos.y < myPos.y - 2) return false;
          return true;
        })
        .sort((a, b2) => ((a.y >= myPos.y ? 0 : 12) + a.distanceTo(myPos)) - ((b2.y >= myPos.y ? 0 : 12) + b2.distanceTo(myPos)));

      let mined = 0;
      for (const pos of candidates.slice(0, 10)) {
        if (mined >= 4 || b.pvp?.target || escapeActive) break;
        const pRes = b.pathfinder.getPathTo(moves, new goals.GoalNear(pos.x, pos.y, pos.z, 2));
        if (!pRes || (pRes.status !== 'success' && pRes.status !== 'partial') || !pRes.path?.length) continue;
        await equipItemByName(b, 'pickaxe');
        await moveNear(b, pos, 2, 10000);
        const blk = b.blockAt(pos);
        if (blk && b.entity.position.distanceTo(pos) <= 4.5) {
          await withTimeout(b.dig(blk), 5000);
          mined++;
          await collectNearbyDrops(b);
        }
      }
      return mined > 0
        ? { ok: true, summary: `${mined}x Stein/Erz (${target || 'stone'}) abgebaut` }
        : { ok: false, summary: `Kein erreichbares ${target || 'Stein/Erz'} innerhalb 36m gefunden` };
    }

    case 'plant_sapling':
    case 'decorate': {
      const decoItem = await equipItemByName(b, action === 'plant_sapling' ? 'sapling' : (target || 'lantern'))
        || await equipItemByName(b, 'torch')
        || await equipItemByName(b, 'poppy')
        || await equipItemByName(b, 'dandelion')
        || await equipItemByName(b, 'sapling');
      if (!decoItem) {
        return { ok: false, summary: `Kein ${action === 'plant_sapling' ? 'Setzling' : 'Deko-Item'} im Inventar` };
      }
      const ground = b.findBlock({
        matching: (blk) => blk && (blk.name === 'grass_block' || blk.name === 'dirt' || blk.name === 'podzol'),
        maxDistance: 14
      });
      if (!ground) return { ok: false, summary: 'Kein geeigneter Bodenplatz im Umkreis' };
      const placed = await placeBlockAt(b, ground.position.offset(0, 1, 0), decoItem.name);
      return placed
        ? { ok: true, summary: `${decoItem.name} platziert` }
        : { ok: false, summary: `Konnte ${decoItem.name} nicht platzieren` };
    }

    case 'hunt_food': {
      // Friendly toward all animals & mobs: forage berries/food drops peacefully instead of attacking creatures
      const beforeFood = countInv(b, (i) => EDIBLE_ITEMS.includes(i.name));
      const berryBushes = b.findBlocks({
        matching: (blk) => blk && blk.name === 'sweet_berry_bush',
        maxDistance: 36,
        count: 6
      });
      let gathered = 0;
      for (const pos of berryBushes.slice(0, 3)) {
        if (escapeActive) break;
        await moveNear(b, pos, 2, 8000);
        const blk = b.blockAt(pos);
        if (blk && blk.name === 'sweet_berry_bush' && b.entity.position.distanceTo(pos) <= 3.5) {
          await withTimeout(b.activateBlock(blk), 2000);
          gathered++;
          await new Promise((r) => setTimeout(r, 250));
          await collectNearbyDrops(b);
        }
      }
      await collectNearbyDrops(b);
      const afterFood = countInv(b, (i) => EDIBLE_ITEMS.includes(i.name));
      const gained = afterFood - beforeFood;
      return gained > 0 || gathered > 0
        ? { ok: true, summary: `Friedlich Beeren/Proviant gesammelt (+${Math.max(0, gained)} Essen)` }
        : { ok: false, summary: 'Keine Beeren oder Essens-Drops innerhalb 36m gefunden' };
    }

    case 'cook_food': {
      const mem = loadMemory();
      if (mem.hutLocation) {
        await moveNear(b, new Vec3(mem.hutLocation.x, mem.hutLocation.y, mem.hutLocation.z), 3, 12000);
      }
      const furnaceBlock = b.findBlock({ matching: (blk) => blk && blk.name === 'furnace', maxDistance: 12 });
      const rawFood = b.inventory.items().find((i) => ['beef', 'porkchop', 'chicken', 'mutton', 'potato', 'cod', 'salmon'].some((k) => i.name === k || i.name === `raw_${k}`));
      const fuel = b.inventory.items().find((i) => i.name.includes('coal') || i.name.includes('_log') || i.name.includes('planks'));
      if (!furnaceBlock) return { ok: false, summary: 'Kein Ofen in Hausnähe gefunden' };
      let cookedTaken = 0;
      try {
        const furnace = await withTimeout(b.openFurnace(furnaceBlock), 3500);
        if (furnace && !furnace._err && !furnace._timeout && typeof furnace.takeOutput === 'function') {
          if (furnace.outputItem()) {
            await withTimeout(furnace.takeOutput(), 2000);
            cookedTaken++;
          }
          if (rawFood && fuel) {
            await withTimeout(furnace.putFuel(fuel.type, null, Math.min(fuel.count, 4)), 2500);
            await withTimeout(furnace.putInput(rawFood.type, null, Math.min(rawFood.count, 4)), 2500);
            await new Promise((r) => setTimeout(r, 10500));
            if (furnace.outputItem()) {
              await withTimeout(furnace.takeOutput(), 2000);
              cookedTaken++;
            }
          }
          furnace.close();
        }
      } catch (e) {}
      await equipItemByName(b, 'cooked') || await equipItemByName(b, 'baked_potato');
      return (cookedTaken > 0 || (rawFood && fuel))
        ? { ok: true, summary: cookedTaken > 0 ? `Gebratenes Essen aus Ofen geholt (${cookedTaken}x)` : 'Essen in Ofen eingelegt' }
        : { ok: false, summary: 'Kein Rohfleisch/Brennstoff oder Ofen leer' };
    }

    case 'eat': {
      const ate = await autoEatIfHungry(b, true);
      if (ate) return { ok: true, summary: `Gegessen (Hunger ${b.food}/20, HP ${Math.round(b.health || 20)}/20)` };
      const food = b.inventory.items().find((i) => EDIBLE_ITEMS.includes(i.name));
      if (!food) return { ok: false, summary: 'Kein Essen im Inventar' };
      return { ok: true, summary: 'Bereits satt' };
    }

    case 'equip_best': {
      const eq = await equipBestGear(b);
      return eq.length > 0
        ? { ok: true, summary: `Ausgerüstet: ${eq.join(', ')}` }
        : { ok: false, summary: 'Keine Waffen oder Rüstung zum Ausrüsten gefunden' };
    }

    case 'visit_player':
    case 'follow_player': {
      const pObj = b.players[target] || Object.values(b.players).find((p) => p.username !== b.username && p.entity);
      if (!pObj || !pObj.entity) return { ok: false, summary: `Spieler ${target || '?'} nicht in Sichtweite` };
      await moveNear(b, pObj.entity.position, 2, 15000);
      await b.lookAt(pObj.entity.position.offset(0, 1.6, 0)).catch(() => {});
      b.swingArm();
      return { ok: true, summary: `${pObj.username} besucht (${Math.round(b.entity.position.distanceTo(pObj.entity.position))}m)` };
    }

    case 'assist_player': {
      assistUntil = Date.now() + 3 * 60 * 1000;
      const pObj = b.players[target] || Object.values(b.players).find((p) => p.username !== b.username && p.entity);
      if (!pObj || !pObj.entity) return { ok: false, summary: `Spieler ${target || '?'} nicht in Sichtweite` };
      await moveNear(b, pObj.entity.position, 3, 12000);
      const treeOrSnow = b.findBlock({
        point: pObj.entity.position,
        matching: (blk) => blk && (blk.name.includes('_log') || blk.name === 'snow'),
        maxDistance: 12
      });
      if (treeOrSnow) {
        await moveNear(b, treeOrSnow.position, 2, 8000);
        await withTimeout(b.dig(treeOrSnow), 4000);
        await collectNearbyDrops(b);
        return { ok: true, summary: `${pObj.username} bei ${treeOrSnow.name} geholfen` };
      }
      return { ok: true, summary: `Bei ${pObj.username} bereit zum Helfen` };
    }

    case 'gift_item': {
      const pObj = b.players[target] || Object.values(b.players).find((p) => p.username !== b.username && p.entity);
      if (!pObj || !pObj.entity) return { ok: false, summary: `Spieler ${target || '?'} nicht erreichbar` };
      await moveNear(b, pObj.entity.position, 2, 12000);
      await b.lookAt(pObj.entity.position.offset(0, 1.5, 0)).catch(() => {});
      const gift = b.inventory.items().find((i) => !i.name.includes('axe') && !i.name.includes('sword') && !i.name.includes('pickaxe') && !i.name.includes('shovel') && !i.name.includes('helmet') && !i.name.includes('chestplate'));
      if (!gift) return { ok: false, summary: 'Kein Geschenk-Item im Inventar' };
      await b.toss(gift.type, null, Math.min(gift.count, 4)).catch(() => {});
      return { ok: true, summary: `${gift.name} an ${pObj.username} verschenkt` };
    }

    case 'sleep': {
      const bed = b.findBlock({
        matching: (blk) => blk && blk.name && blk.name.includes('_bed'),
        maxDistance: 24
      });
      if (!bed) {
        await buildOrMaintainHut(b);
        return { ok: false, summary: 'Kein Bett innerhalb 24m gefunden' };
      }
      await moveNear(b, bed.position, 2, 10000);
      const res = await withTimeout(b.sleep(bed), 3000);
      if (b.isSleeping) {
        sleepingSince = Date.now();
        return { ok: true, summary: 'Im Bett eingeschlafen' };
      }
      return { ok: false, summary: `Schlafen fehlgeschlagen (${res?._err || 'Monster in der Nähe oder Tag'})` };
    }

    case 'celebrate': {
      b.setControlState('jump', true);
      for (let i = 0; i < 4; i++) {
        b.swingArm();
        await new Promise((r) => setTimeout(r, 400));
      }
      b.setControlState('jump', false);
      return { ok: true, summary: 'Kurz gefeiert' };
    }

    case 'run_skill': {
      const saved = loadMemory().learnedSkills?.[target];
      if (!saved || !saved.code) {
        return { ok: false, summary: `Skill '${target}' nicht gefunden` };
      }
      return runCustomSkillCode(b, saved.code, target, saved.description);
    }

    case 'custom_code': {
      return runCustomSkillCode(b, decision.customJs, decision.skillName, decision.skillDescription);
    }

    default:
      b.swingArm();
      return { ok: true, summary: `Aktion ${action} abgeschlossen` };
  }
}

async function runAutonomousStep(triggerChat = null) {
  if (!bot || !bot.entity) return;
  if (isBusy && isBusySince && Date.now() - isBusySince > 35000) {
    log('Watchdog: blockierter Autonomie-Schritt nach >35s zurückgesetzt.');
    isBusy = false;
    isBusySince = 0;
    shelterBusy = false;
    try { bot.pathfinder?.setGoal(null); } catch (e) {}
  }
  const activelyEscaping = escapeActive && Date.now() - lastThreatEscapeAt < 10000;
  if (isBusy) {
    if (triggerChat) {
      pendingChatTrigger = triggerChat;
      try { bot.pathfinder?.setGoal(null); } catch (e) {}
    }
    return;
  }
  if (!triggerChat && (bot.pvp?.target || activelyEscaping || Date.now() - lastThreatEscapeAt < 2500)) return;

  const stepGen = spawnGen;
  const myStepId = ++activeStepId;
  isBusy = true;
  isBusySince = Date.now();
  try {
    await ensureSurvivalGear(bot);
    await equipBestGear(bot);
    await autoEatIfHungry(bot);
    if (bot.entity.isInWater || bot.blockAt(bot.entity.position.floored())?.name === 'water') {
      await tryUnstuck(bot);
    }
    if (!triggerChat && (isNightTime(bot) || loadMemory().nightShelterActive)) {
      if (await ensureNightShelter(bot)) return;
    }
    if (await handleSurvivalThreat(bot)) return;

    const snapshot = collectWorldSnapshot(bot);
    if (!snapshot) return;

    const allowLlm = Boolean(triggerChat) || (Date.now() - lastLlmAt >= LLM_MIN_INTERVAL_MS);
    const decision = await decideNextAction(snapshot, triggerChat, { allowLlm });
    if (decision.usedLlm) {
      lastLlmAt = Date.now();
    }

    if (isNightTime(bot) && isShelteredInPlace(bot) && !isIndoorsAtHome(bot)) {
      if (triggerChat && decision.chat && Date.now() - lastChatReplyAt > 4000) {
        lastChatReplyAt = Date.now();
        bot.chat(decision.chat.slice(0, 220));
      }
      return;
    }
    if (!triggerChat && Object.values(bot.players).filter((p) => p.username !== bot.username && p.entity && p.entity.position.distanceTo(bot.entity.position) <= 48).length === 0) {
      decision.chat = '';
    }
    const outcome = await executeDecision(bot, decision);
    recordActionResult(decision.actionType, decision.target, outcome?.ok !== false, outcome?.summary, outcome?.learnedSkill);
    const logsCount = bot.inventory.items().filter((i) => i.name.includes('_log') || i.name.includes('planks')).reduce((s, i) => s + i.count, 0);
    log(`Step fertig: pos=${bot.entity.position.floored()}, action=${decision.actionType} (${outcome?.ok !== false ? 'OK' : 'FAIL'}: ${outcome?.summary || ''}), HP=${Math.round(bot.health || 20)}/20, Holz=${logsCount}`);
  } catch (err) {
    log(`Fehler im Autonomie-Schritt: ${err.message}`);
  } finally {
    if (stepGen === spawnGen && myStepId === activeStepId) {
      isBusy = false;
      isBusySince = 0;
      if (pendingChatTrigger) {
        const nextChat = pendingChatTrigger;
        pendingChatTrigger = null;
        setTimeout(() => runAutonomousStep(nextChat), 80);
      }
    }
  }
}

function startBot() {
  log(`Verbinde ${BOT_NAME} mit ${BOT_HOST}:${BOT_PORT} (Version ${BOT_VERSION})...`);

  bot = mineflayer.createBot({
    host: BOT_HOST,
    port: BOT_PORT,
    username: BOT_NAME,
    version: BOT_VERSION,
    auth: 'offline',
    checkTimeoutInterval: 60000
  });

  // 1.21.2+ servers (and 26.3 strictly) clear receivedPositionThisTick only on tick_end.
  // mineflayer@4.39.0 omits tick_end, so flush it at the end of every tick/movement turn.
  const MOVEMENT_PACKETS = new Set(['position', 'position_look', 'look', 'flying']);
  const sendsTickEnd = Boolean(require('minecraft-data')(BOT_VERSION)?.supportFeature('sendsClientTickEndPacket'));
  const _origClientWrite = bot._client.write.bind(bot._client);
  let movementSentSinceTickEnd = false;
  let tickEndScheduled = false;

  const flushTickEnd = () => {
    tickEndScheduled = false;
    if (!sendsTickEnd || bot._client.state !== 'play') return;
    movementSentSinceTickEnd = false;
    _origClientWrite('tick_end', {});
  };

  bot._client.write = (name, params) => {
    if (sendsTickEnd && bot._client.state === 'play') {
      if (MOVEMENT_PACKETS.has(name)) {
        if (movementSentSinceTickEnd) {
          movementSentSinceTickEnd = false;
          _origClientWrite('tick_end', {});
        }
        const outParams = params && typeof params.flags === 'object' && params.flags !== null
          ? {
              ...params,
              flags: {
                onGround: Boolean(params.flags.onGround),
                hasHorizontalCollision: Boolean(params.flags.hasHorizontalCollision)
              }
            }
          : params;
        const res = _origClientWrite(name, outParams);
        movementSentSinceTickEnd = true;
        if (!tickEndScheduled) {
          tickEndScheduled = true;
          queueMicrotask(flushTickEnd);
        }
        return res;
      }
      if (name === 'tick_end') {
        movementSentSinceTickEnd = false;
      }
    }
    return _origClientWrite(name, params);
  };

  if (sendsTickEnd) {
    bot.on('physicsTick', () => {
      if (!tickEndScheduled && bot._client.state === 'play') {
        tickEndScheduled = true;
        queueMicrotask(flushTickEnd);
      }
    });
  }

  bot.loadPlugin(pathfinder);
  bot.loadPlugin(collectBlock);
  bot.loadPlugin(pvp);
  // Java server uses float 0.6F / 2 = 0.30000001192092896, while prismarine-physics defaults to 0.3.
  // Using 0.3005 avoids 20Hz FORCED_MOVE rubberbanding when flowing water/movement presses against a wall.
  if (bot.physics) bot.physics.playerHalfWidth = 0.3005;

  const acceptPack = (packet) => {
    log(`Bestätige Server-Resourcepack (${packet?.uuid || 'default'}) automatisch...`);
    try {
      bot._client.write('resource_pack_receive', { uuid: packet?.uuid, result: 3 });
      setTimeout(() => {
        try {
          bot._client.write('resource_pack_receive', { uuid: packet?.uuid, result: 0 });
        } catch (e) {}
      }, 150);
    } catch (e) {}
  };
  bot._client.on('add_resource_pack', acceptPack);
  bot._client.on('resource_pack_send', acceptPack);

  bot.once('spawn', () => {
    mcData = require('minecraft-data')(bot.version);
    if (bot.physics) bot.physics.playerHalfWidth = 0.3005;
    installSafetyGuardrails(bot);
    log(`Erfolgreich im Weihnachtsdorf gespawnt bei ${bot.entity.position}!`);

    if (loopTimer) clearInterval(loopTimer);
    loopTimer = setInterval(() => {
      runAutonomousStep(null);
    }, THINK_INTERVAL_MS);

    if (reflexTimer) clearInterval(reflexTimer);
    reflexTimer = setInterval(() => {
      runSurvivalReflexes(bot).catch(() => {});
    }, 250);
  });

  bot.on('entityHurt', (entity) => {
    if (entity === bot.entity) {
      lastHurtAt = Date.now();
      runSurvivalReflexes(bot).catch(() => {});
    }
  });

  bot.on('spawn', () => {
    lastThreatEscapeAt = 0;
    lastEscapeGoalAt = 0;
    escapeActive = false;
    try { bot.clearControlStates(); } catch (e) {}
    const myGen = spawnGen;
    setTimeout(() => {
      if (spawnGen === myGen && !isBusy) runAutonomousStep(null);
    }, 800);
  });

  let lastTriggerSig = '';
  const handleIncomingChat = (username, message) => {
    if (!username || username === bot.username || username === 'Server') return;
    const sig = `${username}:${message}`;
    if (sig === lastTriggerSig) return;
    lastTriggerSig = sig;
    recentChat.push({ username, message, time: new Date().toLocaleTimeString('de-DE') });
    if (recentChat.length > 20) recentChat.shift();

    const lower = message.toLowerCase();
    const pObj = bot.players[username];
    const isNear = pObj?.entity && bot.entity && pObj.entity.position.distanceTo(bot.entity.position) <= 18;
    const mentionsRudi = lower.includes('rudi') || lower.includes('förster') || lower.includes('foerster') || lower.includes('waldläufer') || lower.includes('waldlaufer');

    if (mentionsRudi || isNear) {
      log(`Spieler-Interaktion von ${username}: "${message}"`);
      runAutonomousStep({ username, message });
    }
  };

  bot.on('chat', (username, message) => {
    if (!bot.players[username]) return;
    handleIncomingChat(username, message);
  });

  bot.on('messagestr', (rawMsg) => {
    if (!rawMsg || rawMsg.includes(bot.username) || /^\s*\[[+-]\]/.test(rawMsg)) return;
    const m = rawMsg.match(/^(?:\[[^\]]+\]\s*)*<([^>]+)>\s*(.+)$/) || rawMsg.match(/^(?:\[[^\]]+\]\s*)*([^:<>]+):\s*(.+)$/);
    if (m) {
      const sender = m[1].replace(/^.*\]\s*/, '').trim();
      const text = m[2].trim();
      if (sender && text && !recentChat.some((c) => c.username === sender && c.message === text)) {
        handleIncomingChat(sender, text);
      }
    }
  });

  bot.on('death', () => {
    spawnGen++;
    isBusy = false;
    pendingChatTrigger = null;
    escapeActive = false;
    lastEscapeGoalAt = 0;
    stopCombat(bot);
    try { bot.clearControlStates(); bot.pathfinder?.setGoal(null); } catch (e) {}
    const mem = loadMemory();
    if (mem.nightShelterActive) {
      mem.nightShelterActive = false;
      saveMemory(mem);
    }
    log('Rudi ist gestorben – respawne und schüttle den Schnee ab...');
  });

  bot.on('kicked', (reason) => {
    log(`Vom Server gekickt: ${JSON.stringify(reason)}`);
  });

  bot.on('error', (err) => {
    log(`Verbindungsfehler: ${err.message}`);
  });

  bot.on('end', () => {
    log('Verbindung getrennt – Reconnect in 10 Sekunden...');
    if (loopTimer) clearInterval(loopTimer);
    if (reflexTimer) clearInterval(reflexTimer);
    stopCombat(bot);
    setTimeout(startBot, 10000);
  });
}

startBot();
