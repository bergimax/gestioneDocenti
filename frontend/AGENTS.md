<!-- LOVABLE:BEGIN -->
> [!IMPORTANT]
> This project is connected to [Lovable](https://lovable.dev). Avoid rewriting
> published git history — force pushing, or rebasing/amending/squashing commits
> that are already pushed — as it rewrites history on Lovable's side and the
> user will likely lose their project history.
>
> Commits you push to the connected branch sync back to Lovable and show up in
> the editor, so keep the branch in a working state.
<!-- LOVABLE:END -->

## Architecture rules

- All backend calls go through the `ApiService` interface in `src/services/` — components import only `api` from `@/services`, never `fetch` directly, so the HTTP and mock implementations stay interchangeable.
- `src/services/mock.ts` is the default implementation; set `VITE_USE_MOCK=false` to use the real HTTP backend. This keeps the frontend runnable with no backend.
