// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
import {analyzeSpectrogram} from './media.mjs';

self.onmessage = ({data}) => {
  try {
    const analysis = analyzeSpectrogram(data, fraction => self.postMessage({type: 'progress', fraction}));
    self.postMessage({type: 'result', analysis}, analysis.panels.map(panel => panel.dbfs.buffer));
  } catch (error) { self.postMessage({type: 'error', message: error.message}); }
};
