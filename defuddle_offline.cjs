// Defuddle receives local HTML only. Block secondary extractor/network requests too.
'use strict';
const blocked = () => { throw new Error('Defuddle networking disabled; use guarded local HTML'); };
globalThis.fetch = async () => blocked();
require('node:net').Socket.prototype.connect = blocked;
require('node:tls').connect = blocked;
for (const name of ['node:http', 'node:https']) {
  const module = require(name);
  module.request = blocked;
  module.get = blocked;
}
require('node:dgram').createSocket = blocked;
