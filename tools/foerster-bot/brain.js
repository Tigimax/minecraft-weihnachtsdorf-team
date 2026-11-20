const fs = require('fs');
const path = require('path');

const MEMORY_PATH = path.join(__dirname, 'memory.json');
const PERSONA_PATH = path.join(__dirname, 'persona.md');

const VALID_ACTIONS = new Set([
  'explore', 'wander', 'build_hut', 'chop_wood', 'craft', 'mine',
  'plant_sapling', 'hunt_food', 'cook_food', 'eat', 'equip_best',
  'decorate', 'visit_player', 'follow_player', 'assist_player',
  'gift_item', 'sleep', 'celebrate', 'custom_code', 'run_skill'
]);

const DEFAULT_MEMORY = {
  botName: 'Waldlaufer_Rudi',
  hutLocation: { x: -6070, y: 81, z: -3223 },
  hutBuilt: true,
  homeGifted: true,
  currentMood: 'wach, neugierig und tatkräftig im Weihnachtsdorf',
  activeProject: {
    hobby: 'Alltag als Förster starten',
    goalSummary: 'Ausrüstung prüfen, Holz/Proviant sichern und die Umgebung pflegen'
  },
  activeDirective: null,
  personalGoals: [
    'Tausend-Fichten-Schwur: Kahle Schneefelder ums Weihnachtsdorf wieder aufforsten',
    'Laternen-Wege: Pfade um (-6070, 81, -3223) und Nachbarhäuser mit Fackeln/Laternen erhellen',
    'Vorrats-Hütte: Steinwerkzeuge, Fichtenholz, Kohle und Beeren für Gäste bereithalten',
    'Dorffreundschaft: Nachbarn wie Tigimax besuchen, mithelfen und kleine Geschenke teilen'
  ],
  planQueue: [],
  actionHistory: [],
  recentGoals: [],
  players: {},
  worldNotes: {},
  learnedSkills: {}
};

function loadMemory() {
  try {
    if (fs.existsSync(MEMORY_PATH)) {
      const raw = JSON.parse(fs.readFileSync(MEMORY_PATH, 'utf8'));
      return {
        ...DEFAULT_MEMORY,
        ...raw,
        activeProject: { ...DEFAULT_MEMORY.activeProject, ...(raw.activeProject || {}) },
        activeDirective: raw.activeDirective && typeof raw.activeDirective === 'object' ? raw.activeDirective : null,
        personalGoals: Array.isArray(raw.personalGoals) && raw.personalGoals.length > 0 ? raw.personalGoals : [...DEFAULT_MEMORY.personalGoals],
        planQueue: Array.isArray(raw.planQueue) ? raw.planQueue : [],
        actionHistory: Array.isArray(raw.actionHistory) ? raw.actionHistory : [],
        recentGoals: Array.isArray(raw.recentGoals) ? raw.recentGoals : [],
        players: raw.players && typeof raw.players === 'object' ? raw.players : {},
        worldNotes: raw.worldNotes && typeof raw.worldNotes === 'object' ? raw.worldNotes : {},
        learnedSkills: raw.learnedSkills && typeof raw.learnedSkills === 'object' ? raw.learnedSkills : {}
      };
    }
  } catch (e) {}
  return JSON.parse(JSON.stringify(DEFAULT_MEMORY));
}

function saveMemory(mem) {
  try {
    fs.writeFileSync(MEMORY_PATH, JSON.stringify(mem, null, 2), 'utf8');
  } catch (e) {}
}

function loadPersona() {
  try {
    if (fs.existsSync(PERSONA_PATH)) {
      return fs.readFileSync(PERSONA_PATH, 'utf8');
    }
  } catch (e) {}
  return 'Du bist Waldläufer Rudi (Waldlaufer_Rudi), der autonome Förster im Minecraft Weihnachtsdorf.';
}

async function callGemini(promptText) {
  const apiKey = (process.env.GEMINI_API_KEY || '').trim();
  if (!apiKey || apiKey === 'YOUR_GEMINI_API_KEY') {
    return null;
  }

  const models = [
    process.env.GEMINI_MODEL || 'gemini-4-hydrogen',
    'gemini-3.5-flash-lite',
    'gemini-3.8-flash'
  ];

  for (const model of models) {
    try {
      const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`;
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: AbortSignal.timeout(7000),
        body: JSON.stringify({
          contents: [{ role: 'user', parts: [{ text: promptText }] }],
          generationConfig: {
            temperature: 0.7,
            maxOutputTokens: 1100,
            responseMimeType: 'application/json'
          }
        })
      });

      if (!res.ok) continue;
      const data = await res.json();
      const raw = data?.candidates?.[0]?.content?.parts?.[0]?.text;
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') return parsed;
      }
    } catch (e) {}
  }
  return null;
}

function sanitizeStep(step, defaultThought = '') {
  if (!step || typeof step !== 'object') return null;
  const actionType = VALID_ACTIONS.has(step.actionType) ? step.actionType : null;
  if (!actionType) return null;
  return {
    thought: String(step.thought || defaultThought || '').slice(0, 220),
    chat: step.chat ? String(step.chat).slice(0, 220) : '',
    actionType,
    target: step.target != null ? String(step.target).trim().slice(0, 80) : '',
    customJs: typeof step.customJs === 'string' ? step.customJs.slice(0, 4000) : '',
    skillName: typeof step.skillName === 'string' ? step.skillName.replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 48) : '',
    skillDescription: typeof step.skillDescription === 'string' ? step.skillDescription.slice(0, 140) : ''
  };
}

function hasInvItem(snapshot, substr) {
  return (snapshot.inventory || []).some((entry) => entry.toLowerCase().includes(substr));
}

function countInvApprox(snapshot, substr) {
  let total = 0;
  for (const entry of snapshot.inventory || []) {
    const m = entry.match(/^(.+)\sx(\d+)$/);
    if (m && m[1].toLowerCase().includes(substr)) {
      total += parseInt(m[2], 10);
    }
  }
  return total;
}

function stepStillRelevant(step, snapshot) {
  if (!step) return false;
  if (step.actionType === 'sleep' && snapshot.timeOfDay === 'Tag') return false;
  if (step.actionType === 'eat' && snapshot.food >= 19 && snapshot.health >= 19) return false;
  if (step.actionType === 'wander' && snapshot.timeOfDay === 'Nacht' && snapshot.health < 14) return false;
  return true;
}

/**
 * Curriculum- & Verification-aware fallback when LLM is unreachable or cooling down.
 */
function fallbackDecision(snapshot, memory, triggerChat) {
  if (triggerChat) {
    const msg = triggerChat.message.toLowerCase();
    const sender = triggerChat.username;

    if (msg.includes('hütte') || msg.includes('huette') || msg.includes('hüte') || msg.includes('haus') || msg.includes('heim')) {
      return {
        hobby: `Auftrag von ${sender}: In die Hütte`,
        thought: `${sender} hat mich in die Hütte geschickt – ich gehe jetzt direkt dorthin.`,
        chat: `Alles klar ${sender}, ich bin direkt auf dem Weg in meine Hütte!`,
        plan: [{ actionType: 'build_hut', target: '', thought: `Zurück in die Hütte für ${sender}.` }]
      };
    }
    if (msg.includes('komm') || msg.includes('einlad') || msg.includes('hierher') || msg.includes('besuch')) {
      return {
        hobby: `Besuch bei ${sender}`,
        thought: `${sender} hat mich eingeladen, ich laufe direkt hin.`,
        chat: `Bin schon auf dem Weg zu dir, ${sender}!`,
        plan: [{ actionType: 'visit_player', target: sender, thought: `Laufe zu ${sender}.` }]
      };
    }
    if (msg.includes('folg') || msg.includes('mitkommen')) {
      return {
        hobby: `${sender} begleiten`,
        thought: `Ich begleite ${sender} ein Stück.`,
        chat: `Alles klar ${sender}, ich komme mit dir mit!`,
        plan: [{ actionType: 'follow_player', target: sender, thought: `Folge ${sender}.` }]
      };
    }
    if (msg.includes('geschenk') || msg.includes('essen') || msg.includes('hunger') || msg.includes('hast du')) {
      return {
        hobby: `Geschenk für ${sender}`,
        thought: `${sender} freut sich über eine Kleinigkeit aus meiner Tasche.`,
        chat: `Warte kurz ${sender}, ich schau mal was ich im Rucksack habe!`,
        plan: [{ actionType: 'gift_item', target: sender, thought: `Gebe ${sender} etwas ab.` }]
      };
    }
    if (msg.includes('hilf') || msg.includes('helfen') || msg.includes('hack') || msg.includes('schnee')) {
      return {
        hobby: `${sender} helfen`,
        thought: `${sender} braucht Hilfe bei der Arbeit.`,
        chat: `Na klar ${sender}, ein Förster packt immer gern mit an!`,
        plan: [{ actionType: 'assist_player', target: sender, thought: `Helfe ${sender}.` }]
      };
    }
    if (msg.includes('holz') || msg.includes('baum') || msg.includes('fäll')) {
      return {
        hobby: `Auftrag von ${sender}: Holz holen`,
        thought: `${sender} wünscht sich Holz – ich kümmere mich darum.`,
        chat: `Mach ich, ${sender}! Ich suche mir direkt Holz.`,
        plan: [{ actionType: 'chop_wood', target: '4', thought: `Holz holen wie von ${sender} gewünscht.` }]
      };
    }
    if (msg.includes('rudi') || msg.includes('förster') || msg.includes('foerster') || msg.includes('hallo') || msg.includes('moin')) {
      return {
        hobby: `Hallo sagen bei ${sender}`,
        thought: `${sender} grüßt mich freundlich.`,
        chat: `Grüß dich ${sender}! Alles bestens im Weihnachtsdorf.`,
        plan: [{ actionType: 'visit_player', target: sender, thought: `Schaue bei ${sender} vorbei.` }]
      };
    }
  }

  const dir = memory.activeDirective;
  if (dir && dir.actionType && Date.now() < (dir.expiresAt || 0)) {
    return {
      hobby: dir.hobby || `Auftrag von ${dir.username}`,
      thought: `Ich führe weiter den Auftrag von ${dir.username} aus: "${dir.message}".`,
      chat: '',
      plan: [{ actionType: dir.actionType, target: dir.target || '', thought: `Aktiven Spieler-Auftrag fortsetzen (${dir.actionType}).` }]
    };
  }

  const lastAction = (memory.actionHistory || [])[0];
  const hasSword = hasInvItem(snapshot, '_sword');
  const hasAxe = hasInvItem(snapshot, '_axe');
  const hasPick = hasInvItem(snapshot, '_pickaxe');
  const logsAndPlanks = countInvApprox(snapshot, '_log') + countInvApprox(snapshot, '_planks');
  const stoneCount = countInvApprox(snapshot, 'cobblestone') + countInvApprox(snapshot, 'cobbled_deepslate');
  const hasRawFood = ['beef', 'porkchop', 'chicken', 'mutton', 'potato'].some((k) => hasInvItem(snapshot, `raw_${k}`) || hasInvItem(snapshot, k));
  const hasCookedFood = ['cooked_', 'bread', 'baked_potato', 'sweet_berries', 'apple'].some((k) => hasInvItem(snapshot, k));

  if ((snapshot.food <= 15 || (snapshot.health < 16 && snapshot.food < 20)) && (hasCookedFood || (hasRawFood && (snapshot.food <= 12 || snapshot.health < 10)))) {
    return {
      hobby: 'Erholen & Stärken',
      thought: 'Ich esse erst einmal etwas, um wieder fit zu sein und zu heilen.',
      chat: '',
      plan: [{ actionType: 'eat', target: '', thought: 'Proviant essen.' }]
    };
  }

  if (snapshot.timeOfDay === 'Nacht') {
    if (!hasSword && logsAndPlanks >= 4) {
      return {
        hobby: 'Nacht-Vorbereitung zuhause',
        thought: 'Nachts crafte ich mir zuerst ein Schwert und ruhe mich dann in der Hütte aus.',
        chat: '',
        plan: [
          { actionType: 'craft', target: 'wooden_sword', thought: 'Holzschwert craften.' },
          { actionType: 'sleep', target: '', thought: 'Im Bett schlafen.' }
        ]
      };
    }
    return {
      hobby: 'Sicher übernachten',
      thought: 'Es ist Nacht – ich schaue zuhause nach dem Rechten und schlafe.',
      chat: '',
      plan: [{ actionType: 'sleep', target: '', thought: 'Zuhause schlafen oder Wache halten.' }]
    };
  }

  // Tech-tree / curriculum priority: avoid repeating immediately failed resource gather!
  const recentFailedChops = (memory.actionHistory || []).slice(0, 4).filter((e) => e.actionType === 'chop_wood' && !e.ok).length;
  const chopFailedRecently = lastAction && lastAction.actionType === 'chop_wood' && !lastAction.ok;
  const mineFailedRecently = lastAction && lastAction.actionType === 'mine' && !lastAction.ok;
  const huntFailedRecently = lastAction && lastAction.actionType === 'hunt_food' && !lastAction.ok;

  if (chopFailedRecently && recentFailedChops >= 2) {
    if (snapshot.food < 19 && !hasCookedFood) {
      return {
        hobby: 'Beeren & Proviant sammeln',
        thought: 'In diesem Sektor gibt es wenig Bäume – ich suche mir stattdessen friedlich Beeren oder Feldfrüchte.',
        chat: '',
        plan: [
          { actionType: 'hunt_food', target: '2', thought: 'Friedlich Beeren/Proviant sammeln.' },
          { actionType: 'explore', target: 'random', thought: 'Weiter Richtung Waldrand ziehen.' }
        ]
      };
    }
    return {
      hobby: 'Stein & Kohle erkunden',
      thought: 'Hier steht kaum Holz – ich schaue nach Stein/Kohle oder ziehe weiter.',
      chat: '',
      plan: [
        { actionType: 'mine', target: 'stone', thought: 'Stein oder Kohle sammeln.' },
        { actionType: 'explore', target: 'random', thought: 'Neuen Waldabschnitt suchen.' }
      ]
    };
  }

  if (chopFailedRecently || mineFailedRecently || huntFailedRecently) {
    const dirs = ['north', 'east', 'south', 'west'];
    const dir = dirs[Math.floor(Math.random() * dirs.length)];
    return {
      hobby: 'Neues Waldrevier erkunden',
      thought: `Hier gab es zuletzt nichts zu holen (${lastAction.summary}) – ich ziehe weiter nach ${dir}.`,
      chat: '',
      plan: [
        { actionType: 'explore', target: dir, thought: `Erkunde Richtung ${dir}.` },
        { actionType: lastAction.actionType, target: lastAction.target || '3', thought: `Versuche ${lastAction.actionType} im neuen Sektor.` }
      ]
    };
  }

  if (logsAndPlanks < 6) {
    return {
      hobby: 'Holzvorrat anlegen',
      thought: 'Ich besorge mir frisches Holz für Werkzeug und Ausrüstung.',
      chat: '',
      plan: [
        { actionType: 'chop_wood', target: '4', thought: '4 Blöcke Holz schlagen.' },
        { actionType: 'craft', target: hasSword ? 'wooden_pickaxe' : 'wooden_sword', thought: 'Grundwerkzeug craften.' }
      ]
    };
  }

  if (!hasSword || !hasAxe || !hasPick) {
    const item = !hasSword ? 'wooden_sword' : (!hasPick ? 'wooden_pickaxe' : 'wooden_axe');
    return {
      hobby: 'Ausrüstung vervollständigen',
      thought: `Mir fehlt noch ${item} – ich crafte es direkt.`,
      chat: '',
      plan: [{ actionType: 'craft', target: item, thought: `${item} herstellen.` }]
    };
  }

  if (stoneCount < 8) {
    return {
      hobby: 'Upgrade auf Stein-Werkzeug',
      thought: 'Ich baue etwas Stein ab, um bessere Werkzeuge zu schmieden.',
      chat: '',
      plan: [
        { actionType: 'mine', target: 'stone', thought: 'Stein und Kohle abbauen.' },
        { actionType: 'craft', target: 'stone_sword', thought: 'Steinschwert craften.' }
      ]
    };
  }

  if (hasRawFood) {
    return {
      hobby: 'Abendessen braten',
      thought: 'Ich habe rohes Fleisch und brate es mir zuhause am Ofen.',
      chat: '',
      plan: [
        { actionType: 'cook_food', target: '', thought: 'Am Ofen Proviant braten und mitnehmen.' },
        { actionType: 'eat', target: '', thought: 'Frisch Gebratenes probieren.' }
      ]
    };
  }

  if (snapshot.food <= 16 && !hasCookedFood) {
    return {
      hobby: 'Beeren & Feldfrüchte sammeln',
      thought: 'Ich suche mir friedlich Beeren, Äpfel oder Feldfrüchte im Dorf.',
      chat: '',
      plan: [
        { actionType: 'hunt_food', target: '2', thought: 'Friedlich Proviant sammeln.' },
        { actionType: 'eat', target: '', thought: 'Proviant essen.' }
      ]
    };
  }

  const hobbies = [
    {
      hobby: 'Wald aufforsten & pflegen',
      thought: 'Als Förster pflanze ich neue Setzlinge und sehe nach dem Wald.',
      plan: [
        { actionType: 'plant_sapling', target: 'sapling', thought: 'Setzling einpflanzen.' },
        { actionType: 'explore', target: 'random', thought: 'Revier abstreifen.' }
      ]
    },
    {
      hobby: 'Weihnachtsdorf verschönern',
      thought: 'Ich dekoriere die Umgebung mit Fackeln/Blumen und erkunde ein Stück.',
      plan: [
        { actionType: 'decorate', target: 'lantern', thought: 'Laterne oder Fackel aufstellen.' },
        { actionType: 'wander', target: '', thought: 'Gemütliche Runde drehen.' }
      ]
    },
    {
      hobby: 'Stein- und Kohlevorrat sichern',
      thought: 'Ich halte nach Kohle und Eisenerz im Fels Ausschau.',
      plan: [
        { actionType: 'mine', target: 'coal', thought: 'Kohle/Stein abbauen.' },
        { actionType: 'build_hut', target: '', thought: 'Nach meiner Hütte schauen.' }
      ]
    }
  ];

  if (snapshot.nearbyPlayers.length > 0 && Math.random() < 0.45) {
    const p = snapshot.nearbyPlayers[0].name;
    return {
      hobby: `Nachbar ${p} besuchen`,
      thought: `Ich schaue mal bei ${p} vorbei.`,
      chat: '',
      plan: [{ actionType: 'visit_player', target: p, thought: `Kurz bei ${p} vorbeischauen.` }]
    };
  }

  const chosen = hobbies[Math.floor(Math.random() * hobbies.length)];
  return { ...chosen, chat: '' };
}

function recordActionResult(actionType, target, ok, summary, learnedSkillCandidate = null) {
  const memory = loadMemory();
  const entry = {
    actionType,
    target: target || '',
    ok: Boolean(ok),
    summary: String(summary || (ok ? 'Erfolg' : 'Fehlgeschlagen')).slice(0, 160),
    at: new Date().toISOString()
  };
  memory.actionHistory = [entry, ...(memory.actionHistory || []).slice(0, 7)];

  // Self-verification: abort queued steps when a prerequisite fails so the planner reflects immediately
  if (!ok) {
    memory.planQueue = [];
  } else if (learnedSkillCandidate && learnedSkillCandidate.name && learnedSkillCandidate.code) {
    const existing = memory.learnedSkills[learnedSkillCandidate.name];
    memory.learnedSkills[learnedSkillCandidate.name] = {
      description: learnedSkillCandidate.description || existing?.description || 'Gelernter Mineflayer-Skill',
      code: learnedSkillCandidate.code,
      uses: (existing?.uses || 0) + 1,
      updatedAt: new Date().toISOString()
    };
  }

  if (memory.activeDirective) {
    const expired = Date.now() >= (memory.activeDirective.expiresAt || 0);
    const sameAction = actionType === memory.activeDirective.actionType;
    const distMatch = actionType === 'build_hut' ? String(summary || '').match(/\((\d+)m Abstand\)/) : null;
    const hutReached = !distMatch || parseInt(distMatch[1], 10) <= 12;
    if (expired || (ok && sameAction && hutReached)) {
      memory.activeDirective = null;
    }
  }
  saveMemory(memory);
  return entry;
}

/**
 * High-level PIANO + Voyager planner:
 * Executes queued steps from the active project until finished/failed or interrupted by chat,
 * then asks Gemini for a new multi-step project/hobby informed by verified actionHistory feedback.
 */
async function decideNextAction(snapshot, triggerChat = null, opts = {}) {
  const memory = loadMemory();
  const allowLlm = opts.allowLlm !== false;

  // 1) Continue active multi-step plan unless a player chats directly
  if (!triggerChat && Array.isArray(memory.planQueue) && memory.planQueue.length > 0) {
    while (memory.planQueue.length > 0) {
      const nextStep = sanitizeStep(memory.planQueue.shift(), memory.activeProject?.goalSummary);
      if (stepStillRelevant(nextStep, snapshot)) {
        saveMemory(memory);
        return {
          ...nextStep,
          fromQueue: true,
          hobby: memory.activeProject?.hobby || ''
        };
      }
    }
    saveMemory(memory);
  }

  const persona = loadPersona();
  const skillSummary = Object.entries(memory.learnedSkills || {})
    .slice(0, 12)
    .map(([name, info]) => `- ${name}: ${info.description || ''} (${info.uses || 1}x genutzt)`)
    .join('\n') || 'noch keine';

  const historySummary = (memory.actionHistory || [])
    .slice(0, 6)
    .map((h) => `- ${h.actionType}(${h.target || '-'}) => ${h.ok ? 'OK' : 'FEHLER'}: ${h.summary}`)
    .join('\n') || 'noch keine';

  const prompt = `${persona}

Du arbeitest wie eine vollautonome Voyager/Mindcraft/PIANO-KI:
- Du setzt dir **eigene Projekte & Hobbys** (2–4 logische Einzelschritte als "plan") und arbeitest sie zielstrebig ab.
- Du prüfst dein echtes Feedback ("Letzte Ergebnisse")! Wenn eine Aktion zuletzt mit FEHLER endete (z. B. kein Baum im Umkreis), wiederhole sie NICHT blind, sondern löse die Ursache (z. B. erst "explore", Werkzeuge craften, essen oder heimgehen).
- Fortschritt wie ein friedlicher Waldläufer: Holz -> Werkbank/Werkzeuge -> Stein -> Kohle/Ofen -> Beeren/Brot/Ernte zubereiten -> Schild/Rüstung zum Schutz -> Wald pflegen / Spieler & Mobs freundlich begegnen / Weihnachtsdorf verschönern. Greife NIEMALS Mobs oder Tiere an!

## Aktuelle Wahrnehmung (Sensoren)
- Position: ${JSON.stringify(snapshot.position)}
- Leben: ${snapshot.health}/20 | Hunger: ${snapshot.food}/20
- Tageszeit: ${snapshot.timeOfDay} | Wetter: ${snapshot.weather}
- Inventar: ${snapshot.inventory.join(', ') || 'leer'}
- Spieler in der Nähe: ${JSON.stringify(snapshot.nearbyPlayers)}
- Entities in der Nähe: ${JSON.stringify(snapshot.nearbyEntities)}
- Blöcke im Umkreis: ${snapshot.nearbyBlocks.join(', ')}
- Letzte Chat-Nachrichten: ${JSON.stringify(snapshot.recentChat)}
${triggerChat ? `\n🔥 DIREKTE NACHRICHT: "${triggerChat.username}" sagt: "${triggerChat.message}" (reagiere freundlich und plane direkt 1–3 passende Schritte!)` : '\n(Kein Chat – plane eigenständig dein nächstes sinnvolles Projekt oder Hobby; "chat" bleibt leer "")'}

## Letzte Ergebnisse (Self-Verification Feedback)
${historySummary}

## Bisheriges Projekt & Gedächtnis
- Persönliche Lebensziele (dein innerer Antrieb): ${JSON.stringify(memory.personalGoals || DEFAULT_MEMORY.personalGoals)}
- Aktives Projekt: ${JSON.stringify(memory.activeProject)}
- Aktiver Spieler-Auftrag (hat Vorrang bis erledigt!): ${memory.activeDirective && Date.now() < (memory.activeDirective.expiresAt || 0) ? JSON.stringify(memory.activeDirective) : 'keiner'}
- Stimmung: ${memory.currentMood}
- Bekannte Spieler: ${JSON.stringify(memory.players)}
- Gelernte Skills (wiederverwendbar via actionType="run_skill"):
${skillSummary}

## Verfügbare Skills (Tools mit target-Parameter)
- "explore": Gezielt 30–45 Blöcke in ein neues Gebiet ziehen (target: "north"|"south"|"east"|"west"|"random")
- "chop_wood": Bäume fällen & Drops einsammeln (target: Blockanzahl z. B. "4")
- "craft": Item/Werkzeug inkl. Vorstufen craften (target: z. B. "wooden_sword", "wooden_pickaxe", "wooden_axe", "stone_sword", "stone_pickaxe", "stone_axe", "shield", "torch", "furnace", "chest", "bread")
- "mine": Stein, Kohle oder Eisenerz abbauen (target: "stone"|"coal"|"iron")
- "hunt_food": Friedlich Beeren, Äpfel oder reife Feldfrüchte sammeln (ohne Mobs/Tiere zu verletzen!) (target: Anzahl z. B. "2")
- "cook_food": Zuhause am Ofen braten & fertiges Essen herausnehmen
- "eat": Vorhandenes Essen verspeisen
- "equip_best": Beste Waffe / Schild / Rüstung anlegen
- "plant_sapling": Setzling nachpflanzen
- "decorate": Laterne/Fackel/Blume schön platzieren (target: "lantern"|"torch"|"flower")
- "build_hut": Zur eigenen Hütte (-6070, 81, -3223) gehen & Basis prüfen
- "sleep": Nachts im eigenen Bett schlafen
- "visit_player" | "follow_player" | "assist_player" | "gift_item": Interaktion mit Spieler <target>
- "wander" | "celebrate": Spazieren / Freuen
- "run_skill": Einen bereits gelernten Skill aus der Skill-Liste oben ausführen (target: skillName)
- "custom_code": Eigenen async Mineflayer-JS-Body schreiben (mit skillName & skillDescription zum Speichern in der Skill-Library! Erhält \`(bot, ctx)\` mit \`ctx.skills\`: \`moveNear, digBlock, placeBlock, collectDrops, craft, equip, log\`).

Antworte AUSSCHLIESSLICH mit validem JSON:
{
  "hobby": "Name deines aktuellen Projekts / Hobbys",
  "thought": "Deine Überlegung inklusive Auswertung der letzten Ergebnisse (1 Satz)",
  "chat": "Nur ausfüllen bei direktem Spieler-Chat, sonst IMMER \"\"",
  "plan": [
    {
      "thought": "Schritt 1 Begründung",
      "actionType": "Tool-Name",
      "target": "Parameter (optional)",
      "skillName": "optional nur bei custom_code",
      "skillDescription": "optional nur bei custom_code",
      "customJs": "optional nur bei custom_code"
    }
  ],
  "memoryUpdate": {
    "currentMood": "aktuelle Stimmung",
    "playerNote": { "name": "optional", "note": "optional" },
    "newGoal": "optional neuer persönlicher Wunsch oder Ziel (kurz)",
    "completedGoal": "optional exakt oder teilweise abgeschlossenes Ziel aus personalGoals"
  }
}`;

  let rawResponse = allowLlm ? await callGemini(prompt) : null;
  let parsed = null;
  if (rawResponse) {
    if (Array.isArray(rawResponse.plan) && rawResponse.plan.length > 0) {
      const steps = rawResponse.plan.map((s) => sanitizeStep(s, rawResponse.thought)).filter(Boolean);
      if (steps.length > 0) {
        parsed = {
          hobby: String(rawResponse.hobby || memory.activeProject?.hobby || 'Förster-Projekt').slice(0, 80),
          thought: String(rawResponse.thought || steps[0].thought || '').slice(0, 220),
          chat: rawResponse.chat ? String(rawResponse.chat).slice(0, 220) : '',
          plan: steps,
          memoryUpdate: rawResponse.memoryUpdate
        };
      }
    } else if (rawResponse.actionType) {
      const single = sanitizeStep(rawResponse);
      if (single) {
        parsed = {
          hobby: String(rawResponse.hobby || memory.activeProject?.hobby || 'Förster-Alltag').slice(0, 80),
          thought: single.thought,
          chat: single.chat,
          plan: [single],
          memoryUpdate: rawResponse.memoryUpdate
        };
      }
    }
  }

  if (!parsed) {
    parsed = fallbackDecision(snapshot, memory, triggerChat);
  }

  if (parsed.memoryUpdate) {
    if (parsed.memoryUpdate.currentMood) {
      memory.currentMood = parsed.memoryUpdate.currentMood;
    }
    if (parsed.memoryUpdate.playerNote?.name && parsed.memoryUpdate.playerNote?.note) {
      memory.players[parsed.memoryUpdate.playerNote.name] = {
        note: parsed.memoryUpdate.playerNote.note,
        updatedAt: new Date().toISOString()
      };
    }
    if (!Array.isArray(memory.personalGoals)) {
      memory.personalGoals = [...DEFAULT_MEMORY.personalGoals];
    }
    if (typeof parsed.memoryUpdate.completedGoal === 'string' && parsed.memoryUpdate.completedGoal.trim()) {
      const doneNeedle = parsed.memoryUpdate.completedGoal.trim().toLowerCase();
      memory.personalGoals = memory.personalGoals.filter((g) => !String(g).toLowerCase().includes(doneNeedle));
    }
    if (typeof parsed.memoryUpdate.newGoal === 'string' && parsed.memoryUpdate.newGoal.trim()) {
      const goal = parsed.memoryUpdate.newGoal.trim();
      if (!memory.personalGoals.some((g) => String(g).toLowerCase() === goal.toLowerCase())) {
        memory.personalGoals.push(goal);
        if (memory.personalGoals.length > 6) memory.personalGoals = memory.personalGoals.slice(-6);
      }
    }
  }

  const [firstStep, ...restSteps] = parsed.plan;
  memory.activeProject = {
    hobby: parsed.hobby || memory.activeProject?.hobby || 'Förster-Alltag',
    goalSummary: parsed.thought || firstStep.thought || ''
  };
  if (triggerChat) {
    memory.activeDirective = {
      username: triggerChat.username,
      message: String(triggerChat.message || '').slice(0, 140),
      actionType: firstStep.actionType,
      target: firstStep.target || '',
      hobby: memory.activeProject.hobby,
      expiresAt: Date.now() + 90000
    };
  }
  memory.planQueue = restSteps.slice(0, 4);
  if (parsed.thought) {
    memory.recentGoals = [parsed.thought, ...(memory.recentGoals || []).slice(0, 5)];
  }
  saveMemory(memory);

  return {
    ...firstStep,
    thought: firstStep.thought || parsed.thought,
    chat: parsed.chat || firstStep.chat || '',
    hobby: memory.activeProject.hobby,
    fromQueue: false,
    usedLlm: Boolean(rawResponse && parsed.plan)
  };
}

module.exports = {
  loadMemory,
  saveMemory,
  recordActionResult,
  decideNextAction,
  fallbackDecision
};
