import fs from 'node:fs/promises';
import path from 'node:path';
import type { Credential, CredentialStore } from '@earendil-works/pi-ai';

export class FileCredentials implements CredentialStore {
  private tail: Promise<unknown> = Promise.resolve();
  constructor(private filename: string) {}
  private async contents(): Promise<Record<string, Credential>> {
    try { return JSON.parse(await fs.readFile(this.filename, 'utf8')); }
    catch (error: any) { if (error.code === 'ENOENT') return {}; throw error; }
  }
  private exclusive<T>(fn: () => Promise<T>): Promise<T> {
    const work = this.tail.then(fn, fn);
    this.tail = work.catch(() => undefined);
    return work;
  }
  private async save(values: Record<string, Credential>) {
    await fs.mkdir(path.dirname(this.filename), { recursive: true, mode: 0o700 });
    const temp = `${this.filename}.tmp`;
    const handle = await fs.open(temp, 'w', 0o600);
    try { await handle.writeFile(JSON.stringify(values)); await handle.sync(); }
    finally { await handle.close(); }
    await fs.rename(temp, this.filename);
  }
  async read(provider: string) { await this.tail; return (await this.contents())[provider]; }
  async list() {
    await this.tail;
    return Object.entries(await this.contents()).map(([providerId, credential]) => ({ providerId, type: credential.type }));
  }
  async modify(provider: string, fn: (current: Credential | undefined) => Promise<Credential | undefined>) {
    return this.exclusive(async () => {
      const values = await this.contents();
      const value = await fn(values[provider]);
      if (value !== undefined) { values[provider] = value; await this.save(values); }
      return values[provider];
    });
  }
  async delete(provider: string) {
    await this.exclusive(async () => { const values = await this.contents(); delete values[provider]; await this.save(values); });
  }
}
