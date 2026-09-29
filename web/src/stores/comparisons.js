import { defineStore } from 'pinia';
import { mergePacket, packetKey } from '../utils/comparisons.js';

export const useComparisonStore = defineStore('comparisons', {
  state: () => ({ latest: new Map() }),
  actions: {
    receive(packet) {
      if (!packet?.token || !packet?.chainId || !Array.isArray(packet.pairs)) return false;
      const key = packetKey(packet);
      this.latest.set(key, mergePacket(this.latest.get(key), packet));
      return true;
    },
  },
});
