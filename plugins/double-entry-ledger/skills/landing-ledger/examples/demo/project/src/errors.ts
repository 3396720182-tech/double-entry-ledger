export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function notFound(): HttpError {
  return new HttpError(404, 'not found');
}

export function gone(): HttpError {
  return new HttpError(410, 'gone');
}
