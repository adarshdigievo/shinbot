import fs from 'node:fs/promises';
import path from 'node:path';

export type CustomModel = {
  id: string;
  name?: string;
  baseUrl: string;
  model: string;
  contextWindow?: number;
  requiresKey?: boolean;
};

export class CustomModelStore {
  private tail: Promise<unknown> = Promise.resolve();
  constructor(private filename: string) {}

  private async contents(): Promise<CustomModel[]> {
    try { return JSON.parse(await fs.readFile(this.filename, 'utf8')); }
    catch (error: any) { if (error.code === 'ENOENT') return []; throw error; }
  }

  async list() {
    await this.tail;
    return this.contents();
  }

  async upsert(item: CustomModel) {
    const update = async () => {
      const values = (await this.contents()).filter(value => value.id !== item.id);
      values.push(item);
      await fs.mkdir(path.dirname(this.filename), { recursive: true, mode: 0o700 });
      const temporary = `${this.filename}.tmp`;
      const handle = await fs.open(temporary, 'w', 0o600);
      try {
        try { await handle.writeFile(JSON.stringify(values)); await handle.sync(); }
        finally { await handle.close(); }
        await fs.rename(temporary, this.filename);
        const directory = await fs.open(path.dirname(this.filename), 'r');
        try { await directory.sync(); } finally { await directory.close(); }
      } finally { await fs.rm(temporary, { force: true }); }
    };
    const work = this.tail.then(update, update);
    this.tail = work.catch(() => undefined);
    await work;
  }
}
