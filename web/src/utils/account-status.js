export function telegramDeliveryState({ telegram, health, preferences, loading, error, healthError, now = Date.now(), healthReadAt = now }) {
  if (loading) return 'loading';
  if (error || !telegram) return 'error';
  if (!telegram.available) return 'unavailable';
  if (!telegram.linked) return 'unlinked';
  if (!preferences) return 'rules-unconfirmed';
  if (!preferences.newPool && !preferences.riskChange && !(preferences.largeTrade && telegram.capabilities?.largeTrade?.available)) return 'paused';
  if (healthError || !health) return 'health-error';
  const at=Number(health.worker?.updatedAt),elapsed=Math.max(0,now-healthReadAt);
  const age=health.workerAgeMs==null ? now-at : Number(health.workerAgeMs)+elapsed;
  if (!at || at>now+60000 || !Number.isFinite(age) || age<0 || age>60000) return 'delayed';
  if (!health.configured || health.worker?.status!=='ready') return 'service-unavailable';
  return 'ready';
}
