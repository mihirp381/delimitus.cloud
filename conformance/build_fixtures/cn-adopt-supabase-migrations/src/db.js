import { createClient } from '@supabase/supabase-js';

// A store the builder provisioned for this app. Its schema is in
// supabase/migrations/ in this same repository, it was created by this app,
// and no other system reads or writes it.
export const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL,
  import.meta.env.VITE_SUPABASE_ANON_KEY,
);

export const listSessions = () => supabase.from('sessions').select('*').order('starts_at');
export const submitSignup = (row) => supabase.from('signups').insert(row);
