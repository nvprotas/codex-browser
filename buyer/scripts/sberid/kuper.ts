import { writeFileSync } from 'node:fs';

function arg(name: string): string {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) {
    throw new Error(`Missing argument: ${name}`);
  }
  return process.argv[index + 1]!;
}

const outputPath = arg('--output-path');

const payload = {
  status: 'failed',
  reason_code: 'auth_refresh_requested',
  message: 'Скрипт kuper в состоянии draft и не должен запускаться в production.',
  artifacts: {
    script: 'kuper',
    lifecycle: 'draft',
  },
};

writeFileSync(outputPath, JSON.stringify(payload, null, 2), { encoding: 'utf-8' });
process.stdout.write(`${JSON.stringify(payload)}\n`);
