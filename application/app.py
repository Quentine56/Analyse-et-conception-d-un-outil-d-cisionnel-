import streamlit as st
import pandas as pd
import psycopg2
from sqlalchemy import create_engine
import re
from datetime import date

# -----------------------------------------------------------------------------
# 1. CONFIGURATION & CONNEXION (Adaptée au format WIN1252 du backup)
# -----------------------------------------------------------------------------
DB_CONFIG = {
    "dbname": "MD", # Nom de la base identifié dans le backup [cite: 4]
    "user": "pgis",
    "password": "pgis", 
    "host": "localhost",
    "port": "5437"
}

# Utilisation de SQLAlchemy avec forçage de l'encodage WIN1252 [cite: 2]
# Cela évite les erreurs de décodage des caractères accentués
conn_url = f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}?client_encoding=win1252"
engine = create_engine(conn_url)

# -----------------------------------------------------------------------------
# 2. LOGIQUE DE PARSING INTELLIGENTE
# -----------------------------------------------------------------------------

def parse_comment_to_dict(comment):
    """
    Extrait les choix en ignorant les parenthèses de texte comme 'Enfant(s)'.
    Cible spécifiquement les listes de type (1:A; 2:B) ou (1;2;3).
    """
    if not comment: return {}
    
    # Nettoyage : On ignore ce qui vient après ", Rubrique" [cite: 17, 41]
    text_to_parse = comment.split(', Rubrique')[0]
    
    # Regex : on cherche tout ce qui est entre parenthèses
    # On privilégie le bloc qui contient des ';' ou des ':'
    matches = re.findall(r'\(([^()]+)\)', text_to_parse)
    
    candidate = None
    for m in matches:
        if ';' in m or ':' in m:
            candidate = m
            break
    
    if not candidate: return {}
            
    mapping = {}
    try:
        items = candidate.split(';')
        for item in items:
            item = item.strip()
            if not item: continue
            if ':' in item:
                parts = item.split(':', 1)
                mapping[parts[0].strip()] = parts[1].strip()
            else:
                mapping[item] = item
        return mapping
    except Exception:
        return {}

@st.cache_data(ttl=60)
def get_table_metadata(table_name):
    """Récupère la structure et gère les rubriques du backup [cite: 17-41]"""
    query = f"""
        SELECT a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS data_type,
               col_description(a.attrelid, a.attnum) AS comment, a.attnotnull AS is_required
        FROM pg_attribute a JOIN pg_class c ON a.attrelid = c.oid
        WHERE c.relname = '{table_name.lower()}' AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum;
    """
    try:
        df = pd.read_sql(query, engine)
        structure = []
        for _, row in df.iterrows():
            if row['column_name'] in ['num', 'pos']: continue
            comment = row['comment'] or ""
            # Extraction de la rubrique (ex: 'Rubrique Usager' -> 'Usager') [cite: 24]
            rubrique = comment.split("Rubrique ")[-1].strip() if "Rubrique " in comment else "Général"
            
            structure.append({
                "name": row['column_name'],
                "type": row['data_type'],
                "required": row['is_required'],
                "choices": parse_comment_to_dict(comment),
                "rubrique": rubrique
            })
        return structure
    except Exception as e:
        st.error(f"Erreur lors de la lecture des métadonnées : {e}")
        return []

# -----------------------------------------------------------------------------
# 3. SAUVEGARDE TRANSACTIONNELLE (ENTRETIEN, DEMANDE, SOLUTION)
# -----------------------------------------------------------------------------

def save_data(ent_data, list_dem, list_sol, dict_dem, dict_sol):
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.set_client_encoding('WIN1252') # Alignement avec le backup [cite: 2]
        cur = conn.cursor()

        # 1. Insertion ENTRETIEN 
        cols = ent_data.keys()
        vals = [ent_data[c] for c in cols]
        query_ent = f"INSERT INTO public.entretien ({', '.join(cols)}) VALUES ({', '.join(['%s']*len(cols))}) RETURNING num"
        cur.execute(query_ent, vals)
        new_id = cur.fetchone()[0]

        # 2. Insertion DEMANDES [cite: 15]
        for i, val in enumerate(list_dem):
            code_dem = next((k for k, v in dict_dem.items() if v == val), val)
            cur.execute("INSERT INTO public.demande (num, pos, nature) VALUES (%s, %s, %s)", (new_id, i+1, code_dem))

        # 3. Insertion SOLUTIONS [cite: 56]
        for i, val in enumerate(list_sol):
            code_sol = next((k for k, v in dict_sol.items() if v == val), val)
            cur.execute("INSERT INTO public.solution (num, pos, nature) VALUES (%s, %s, %s)", (new_id, i+1, code_sol))

        conn.commit()
        st.success(f"✅ Dossier n°{new_id} enregistré !")
    except Exception as e:
        if conn: conn.rollback()
        st.error(f"Erreur SQL : {e}")
    finally:
        if conn: conn.close()

# -----------------------------------------------------------------------------
# 4. INTERFACE STREAMLIT
# -----------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Maison du Droit", layout="wide")
    st.title("⚖️ Gestion Maison du Droit - Vannes")

    # Structures issues du backup [cite: 18, 15, 56]
    struct_ent = get_table_metadata("entretien")
    struct_dem = get_table_metadata("demande")
    struct_sol = get_table_metadata("solution")

    if not struct_ent: st.stop()

    choice = st.sidebar.radio("Navigation", ["Ajouter Entretien", "Voir Données"])

    if choice == "Ajouter Entretien":
        with st.form("form_global", clear_on_submit=True):
            rubriques = sorted(list(set(col['rubrique'] for col in struct_ent)))
            tabs = st.tabs(rubriques + ["Demandes & Solutions"])
            form_data = {}

            for i, rub in enumerate(rubriques):
                with tabs[i]:
                    fields = [f for f in struct_ent if f['rubrique'] == rub]
                    cols = st.columns(2)
                    for j, f in enumerate(fields):
                        label = f"{f['name'].capitalize()} {'*' if f['required'] else ''}"
                        curr_col = cols[j % 2]
                        if f['choices']:
                            sel = curr_col.selectbox(label, list(f['choices'].values()), key=f"ent_{f['name']}")
                            form_data[f['name']] = next((k for k, v in f['choices'].items() if v == sel), None)
                        elif 'date' in f['type']:
                            form_data[f['name']] = curr_col.date_input(label, key=f"ent_{f['name']}")
                        elif 'int' in f['type']:
                            form_data[f['name']] = curr_col.number_input(label, min_value=0, step=1, key=f"ent_{f['name']}")
                        else:
                            form_data[f['name']] = curr_col.text_input(label, key=f"ent_{f['name']}")

            with tabs[-1]:
                dict_dem = struct_dem[0]['choices'] if struct_dem else {}
                sel_dem = st.multiselect("Natures des Demandes", list(dict_dem.values()))
                dict_sol = struct_sol[0]['choices'] if struct_sol else {}
                sel_sol = st.multiselect("Natures des Solutions", list(dict_sol.values()))

            if st.form_submit_button("💾 ENREGISTRER L'ENTRETIEN", use_container_width=True):
                save_data(form_data, sel_dem, sel_sol, dict_dem, dict_sol)

    else:
        st.header("Visualisation")
        df = pd.read_sql("SELECT * FROM public.entretien ORDER BY num DESC", engine)
        st.dataframe(df, use_container_width=True)

if __name__ == "__main__":
    main()