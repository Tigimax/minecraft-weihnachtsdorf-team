const fs = require('fs');
const nmp = require('minecraft-protocol');

const workerId = process.argv[2] || '1';
const startId = parseInt(process.argv[3] || '1', 10);
const count = parseInt(process.argv[4] || '20', 10);

const posFile = `/tmp/worker_${workerId}_pos.json`;
const bots = {};
let shuttingDown = false;

console.log(`[Worker ${workerId}] Spawning ${count} protocol bots (${startId} bis ${startId + count - 1})...`);

for (let i = 0; i < count; i++) {
  const botId = startId + i;
  const username = `StressBot${botId.toString().padStart(3, '0')}`;

  setTimeout(() => {
    if (!shuttingDown) createBotSession(botId, username);
  }, i * 250);
}

function createBotSession(botId, username) {
  if (shuttingDown) return;

  const client = nmp.createClient({
    host: '192.168.1.148',
    port: 25565,
    username: username,
    version: '1.20.4',
    auth: 'offline',
    keepAlive: true
  });

  const session = {
    id: botId,
    username: username,
    client: client,
    pos: { x: -31.5, y: 77.0, z: 0.5, yaw: 0, pitch: 0 },
    basePos: null,
    angle: Math.random() * Math.PI * 2,
    active: false
  };

  client.on('login', () => {
    session.active = true;
  });

  client.on('position', (packet) => {
    try {
      client.write('teleport_confirm', { teleportId: packet.teleportId });
      session.pos.x = packet.x;
      session.pos.y = packet.y;
      session.pos.z = packet.z;
      session.pos.yaw = packet.yaw;
      session.pos.pitch = packet.pitch;

      if (!session.basePos || Math.abs(packet.x - (-31.5)) > 5) {
        session.basePos = { x: packet.x, y: packet.y, z: packet.z };
      }
    } catch (e) {}
  });

  client.on('error', () => {
    session.active = false;
  });

  client.on('end', () => {
    session.active = false;
    if (!shuttingDown) {
      setTimeout(() => {
        if (!shuttingDown) createBotSession(botId, username);
      }, 3000);
    }
  });

  bots[botId] = session;
}

// Active simulation loop: Every 800ms, simulate real movement & arm swings
setInterval(() => {
  for (const botId in bots) {
    const s = bots[botId];
    if (s && s.active && s.client && s.client.state === 'play') {
      try {
        s.angle += 0.25;
        const radius = 2.5;
        const targetX = s.basePos ? s.basePos.x + Math.cos(s.angle) * radius : s.pos.x;
        const targetZ = s.basePos ? s.basePos.z + Math.sin(s.angle) * radius : s.pos.z;
        const targetYaw = s.angle * (180 / Math.PI);

        s.pos.x = targetX;
        s.pos.z = targetZ;
        s.pos.yaw = targetYaw;

        s.client.write('position_look', {
          x: s.pos.x,
          y: s.pos.y,
          z: s.pos.z,
          yaw: s.pos.yaw,
          pitch: 0,
          onGround: true
        });

        if (Math.random() > 0.5) {
          s.client.write('arm_animation', { hand: 0 });
        }
      } catch (e) {}
    }
  }
}, 800);

// Live BlueMap coordinate export every 2 seconds (ONLY strictly connected bots)
setInterval(() => {
  const activeBots = [];
  for (const botId in bots) {
    const s = bots[botId];
    if (s && s.active && s.client && s.client.state === 'play') {
      activeBots.push({
        id: s.id,
        name: s.username,
        x: s.pos.x,
        y: s.pos.y,
        z: s.pos.z,
        yaw: s.pos.yaw
      });
    }
  }
  try {
    fs.writeFileSync(posFile, JSON.stringify(activeBots));
  } catch (e) {}
}, 2000);

function cleanup() {
  shuttingDown = true;
  try { if (fs.existsSync(posFile)) fs.unlinkSync(posFile); } catch (e) {}
  for (const botId in bots) {
    const s = bots[botId];
    if (s && s.client) {
      try { s.client.end(); } catch (e) {}
    }
  }
  setTimeout(() => process.exit(0), 400);
}

process.on('SIGINT', cleanup);
process.on('SIGTERM', cleanup);
