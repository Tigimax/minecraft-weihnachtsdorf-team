#!/usr/bin/env php
<?php
declare(strict_types=1);

/**
 * GFS Backup Pruner for Pterodactyl:
 * Keeps 12 newest unlocked backups (hourly) + 12 half-daily backups (2/day, 6 days) = 24 total.
 * Locked backups (is_locked=1) are never touched.
 *
 * Deployed to: mc-node1:/usr/local/bin/ptero-backup-prune.php
 * Cron (www-data): 5 * * * * /usr/bin/php /usr/local/bin/ptero-backup-prune.php 2 >> /var/log/ptero-backup-prune.log 2>&1
 */
function selectBackupsToDelete(array $items, int $hourlyKeep = 12, int $halfDailyKeep = 12, string $tz = 'Europe/Berlin'): array {
    // $items must be sorted newest-first: [['id' => ..., 'ts' => int], ...]
    if (count($items) <= $hourlyKeep) {
        return [];
    }
    $keepIds = [];
    // Tier 1: newest $hourlyKeep backups
    foreach (array_slice($items, 0, $hourlyKeep) as $it) {
        $keepIds[$it['id']] = true;
    }
    // Tier 2: remaining backups grouped by 12h bucket (AM/PM in $tz), keeping earliest per bucket
    $buckets = [];
    $tzObj = new DateTimeZone($tz);
    foreach (array_slice($items, $hourlyKeep) as $it) {
        $dt = (new DateTimeImmutable('@' . $it['ts']))->setTimezone($tzObj);
        $half = ((int)$dt->format('G') < 12) ? 'AM' : 'PM';
        $key = $dt->format('Y-m-d') . '-' . $half;
        $buckets[$key] = $it;
    }
    krsort($buckets);
    foreach (array_slice($buckets, 0, $halfDailyKeep, true) as $chosen) {
        $keepIds[$chosen['id']] = true;
    }
    $deleteIds = [];
    foreach ($items as $it) {
        if (!isset($keepIds[$it['id']])) {
            $deleteIds[] = $it['id'];
        }
    }
    return $deleteIds;
}

if (in_array('--self-test', $argv, true)) {
    $live = [];
    $base = strtotime('2026-11-26 00:00:00 Europe/Berlin');
    for ($h = 0; $h < 240; $h++) {
        array_unshift($live, ['id' => $h, 'ts' => $base + $h * 3600]);
        $del = array_flip(selectBackupsToDelete($live, 12, 12));
        $live = array_values(array_filter($live, fn($x) => !isset($del[$x['id']])));
        assert(count($live) <= 24, "Exceeded 24 at hour $h: " . count($live));
    }
    assert(count($live) === 24, "Expected 24 after 240h, got " . count($live));
    for ($i = 0; $i < 12; $i++) {
        assert($live[$i]['id'] === 239 - $i, "Hourly slot $i mismatch");
    }
    $tzObj = new DateTimeZone('Europe/Berlin');
    for ($i = 12; $i < 24; $i++) {
        $hr = (int)(new DateTimeImmutable('@' . $live[$i]['ts']))->setTimezone($tzObj)->format('G');
        assert($hr === 0 || $hr === 12, "Half-daily slot $i hour=$hr");
    }
    echo "SELF-TEST PASSED: 240h simulation -> exact 12 hourly + 12 half-daily (00:00/12:00) = 24 backups\n";
    exit(0);
}

$serverId = (int)($argv[1] ?? 2);
$dryRun = in_array('--dry-run', $argv, true);

require '/var/www/pterodactyl/vendor/autoload.php';
$app = require_once '/var/www/pterodactyl/bootstrap/app.php';
$kernel = $app->make(Illuminate\Contracts\Console\Kernel::class);
$kernel->bootstrap();

$backups = Pterodactyl\Models\Backup::query()
    ->where('server_id', $serverId)
    ->where('is_successful', true)
    ->where('is_locked', false)
    ->whereNotNull('completed_at')
    ->whereNull('deleted_at')
    ->orderByDesc('created_at')
    ->get();

$items = [];
$byId = [];
foreach ($backups as $b) {
    $items[] = ['id' => $b->id, 'ts' => $b->created_at->getTimestamp()];
    $byId[$b->id] = $b;
}

$deleteIds = selectBackupsToDelete($items, 12, 12, 'Europe/Berlin');
$deleteService = $app->make(Pterodactyl\Services\Backups\DeleteBackupService::class);

echo sprintf("[ptero-backup-prune] server=%d unlocked=%d delete=%d dry_run=%s\n",
    $serverId, count($items), count($deleteIds), $dryRun ? 'yes' : 'no');

foreach ($deleteIds as $id) {
    $b = $byId[$id];
    echo sprintf("  %s backup id=%d uuid=%s created=%s name=%s\n",
        $dryRun ? 'WOULD DELETE' : 'DELETING', $b->id, $b->uuid, $b->created_at, $b->name);
    if (!$dryRun) {
        $deleteService->handle($b);
    }
}
