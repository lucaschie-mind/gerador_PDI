import pandas as pd
import streamlit as st
import numpy as np
import requests
import psycopg2
import os
import unicodedata
import datetime as dt
from typing import Optional, Tuple, Literal
from psycopg2 import sql as psql
from psycopg2.extras import RealDictCursor
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from datetime import datetime, date, timedelta

def _get_cfg(name, required=False, default=None):
    # tenta env; se existir st.secrets localmente, tenta também
    val = os.getenv(name)
    if val is None and hasattr(st, "secrets"):
        try:
            val = st.secrets.get(name)
        except Exception:
            val = None
    if (val is None or str(val).strip() == "") and required:
        st.error(f"⚠️ Variável '{name}' não está definida. Configure em Railway → Settings → Variables.")
        st.stop()
    return val if (val is not None and str(val).strip() != "") else default

# OBRIGATÓRIAS
DATABASE_URL = _get_cfg("DATABASE_URL", required=True)
API_URL      = _get_cfg("API_URL",      required=True)

# OPCIONAL (se não tiver, usa a mesma do principal)
DATABASE_URL_RESUMO_SEMANAL = _get_cfg("DATABASE_URL_RESUMO_SEMANAL", required=False)

# (se você envia e-mail pelo Graph)
CLIENT_ID     = _get_cfg("CLIENT_ID",     required=False)
CLIENT_SECRET = _get_cfg("CLIENT_SECRET", required=False)
TENANT_ID     = _get_cfg("TENANT_ID",     required=False)
SENDER_EMAIL  = _get_cfg("SENDER_EMAIL",  required=False)


foco = 'pdi'

# ==== TELA DE LOGIN ====
def autenticar_usuario(email, senha):
    try:
        with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT id
                FROM pessoas_ativos
                WHERE email = %s
                LIMIT 1;
            """, (email,))
            row = cur.fetchone()
            if row:
                id_banco = str(row[0]).strip()
                return id_banco == senha.strip()
            return False
    except Exception as e:
        st.error(f"[ERRO] Falha ao conectar no banco: {e}")
        return False


# Se ainda não autenticou, mostra a tela de login
if "autenticado" not in st.session_state or not st.session_state["autenticado"]:
    st.title("Login - PDI Mindsight")

    input_email = st.text_input("Digite seu e-mail")
    input_senha = st.text_input("Digite seu ID (senha)", type="password")

    if st.button("Entrar"):
        if autenticar_usuario(input_email, input_senha):
            st.session_state["autenticado"] = True
            st.session_state["email"] = input_email
            st.success("Login realizado com sucesso!")
            st.rerun()
        else:
            st.error("Seu id não está correto, verifique no FULL o seu código pela URL, ou entre em contato com o Lucas.")
    st.stop()

# ==== A PARTIR DAQUI, O RESTANTE DO SCRIPT ====
email = st.session_state["email"]

#Parametros:
delta_tempo_resumo = 45 # dias
delta_tempo = 90       # dias
tempo_atualizacao = 180  # dias


# ==== TIPOS ====
INFO_TAGS_PF   = "tags pontos fortes"
INFO_TAGS_PD   = "tags pontos desenvolvimento"
INFO_OBJETIVOS = "objetivos de carreira"
INFO_TAREFAS   = "tarefas cargo (autoavaliação)"
INFO_DIAGNOSTICO = "diagnostico pdi"

TIPOS_CANON = [
    INFO_TAGS_PF,
    INFO_TAGS_PD,
    "resumo avd",
    "output_feedback",
    "output_pdi",
    INFO_OBJETIVOS,
    INFO_TAREFAS,
    INFO_DIAGNOSTICO,
]

# ==== HELPERS ====
def _is_empty_text(x): return x is None or str(x).strip() == ""
def _parse_data(dt_):
    if isinstance(dt_, datetime): return dt_.date()
    if isinstance(dt_, date): return dt_
    if isinstance(dt_, str):
        try: return datetime.fromisoformat(dt_).date()
        except: return None
    return None
def _dias_desde(d): return None if d is None else (date.today() - d).days

# ==== BANCO ====
def _descobrir_tabela(conn, alvo="dados_AVD_pessoas"):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT schemaname, tablename
            FROM pg_catalog.pg_tables
            WHERE lower(tablename) = lower(%s)
            ORDER BY (schemaname = 'public') DESC, schemaname, tablename
            LIMIT 1;
        """, (alvo,))
        row = cur.fetchone()
        if row: return row[0], row[1]
        raise RuntimeError("Tabela não encontrada")

def salvar_info(email: str, informacao: str, descricao: str):
    if _is_empty_text(descricao):
        return
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            schema, table = _descobrir_tabela(conn)
            tbl = psql.SQL("{}.{}").format(psql.Identifier(schema), psql.Identifier(table))
            query = psql.SQL("""
                INSERT INTO {tbl} (email, informacao, descricao, data)
                VALUES (%s, %s, %s, %s);
            """).format(tbl=tbl)
            with conn.cursor() as cur:
                cur.execute(query, (email, informacao, descricao.strip(), datetime.now()))
            conn.commit()
        st.success(f"[OK] {informacao} salvo.")
    except Exception as e:
        st.error(f"[ERRO] Falha ao salvar {informacao}: {e}")

def get_latest_infos(email: str):
    valores = {t: "" for t in TIPOS_CANON}
    datas   = {t: None for t in TIPOS_CANON}
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            schema, table = _descobrir_tabela(conn)
            tbl = psql.SQL("{}.{}").format(psql.Identifier(schema), psql.Identifier(table))
            query = psql.SQL("""
                SELECT DISTINCT ON (info_norm)
                       info_norm, descricao, data
                FROM (
                    SELECT trim(lower(informacao)) AS info_norm,
                           descricao, data
                    FROM {tbl}
                    WHERE email = %s
                ) t
                ORDER BY info_norm, data DESC NULLS LAST;
            """).format(tbl=tbl)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, (email,))
                rows = cur.fetchall()
        tipos_alvo = {t.strip().lower(): t for t in TIPOS_CANON}
        for r in rows:
            info_norm = (r.get("info_norm") or "").strip().lower()
            if info_norm in tipos_alvo:
                canon = tipos_alvo[info_norm]
                valores[canon] = r.get("descricao") or ""
                datas[canon]   = r.get("data")
    except Exception as e:
        st.error(f"[ERRO] Falha ao acessar banco: {e}")
    return (
        valores[INFO_TAGS_PF], datas[INFO_TAGS_PF],
        valores[INFO_TAGS_PD], datas[INFO_TAGS_PD],
        valores["resumo avd"], datas["resumo avd"],
        valores["output_feedback"], datas["output_feedback"],
        valores["output_pdi"], datas["output_pdi"],
        valores[INFO_OBJETIVOS], datas[INFO_OBJETIVOS],
        valores[INFO_TAREFAS], datas[INFO_TAREFAS],
        valores[INFO_DIAGNOSTICO], datas[INFO_DIAGNOSTICO],
    )

def obter_token_graph():
    """Autentica no Azure AD e retorna um token de acesso válido"""
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials"
    }
    resp = requests.post(url, data=data)
    resp.raise_for_status()
    return resp.json()["access_token"]

def enviar_email_graph(destinatario: str, assunto: str, corpo: str):
    """Envia um e-mail pelo Microsoft Graph"""
    token = obter_token_graph()
    url = f"https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    message = {
        "message": {
            "subject": assunto,
            "body": {
                "contentType": "Text",
                "content": corpo
            },
            "toRecipients": [
                {"emailAddress": {"address": destinatario}}
            ]
        }
    }
    resp = requests.post(url, headers=headers, json=message)
    if resp.status_code in (200, 202):
        return True
    else:
        st.error(f"[ERRO] Falha ao enviar email: {resp.text}")
        return False

# ==== CONSULTAS AUXILIARES ====
# resumo_pessoa, cargo_pessoa, id_pessoa
resumo_pessoa, cargo_pessoa, id_pessoa = None, None, None
try:
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        SELECT resumo_pessoa, id, posicao
        FROM pessoas_ativos
        WHERE email = %s
        LIMIT 1;
    """, (email,))
    result = cur.fetchone()
    if result:
        resumo_pessoa, id_pessoa, cargo_pessoa = result
    cur.close()
    conn.close()
except Exception as e:
    st.error(f"[ERRO] Falha ao buscar resumo_pessoa: {e}")

# histórico bot
historico_bot = ""
try:
    data_limite = date.today() - timedelta(days=delta_tempo_resumo)
    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT data, output_pessoa_bot
            FROM outputs_bot_pessoas
            WHERE email = %s
              AND data >= %s
            ORDER BY data DESC
            LIMIT 5;
        """, (email, data_limite))
        rows = cur.fetchall() or []
        if rows:
            historico_bot = '; '.join(
                f"data: {d.strftime('%Y-%m-%d')} - resumo: {resumo or ''}" for d, resumo in rows
            )
        else:
            historico_bot = "Não há nenhuma interação até o momento"
except Exception as e:
    st.error(f"[ERRO] Falha ao buscar historico_bot: {e}")

# resumos semanais
resumos_semanal = ""
try:
    engine = create_engine(DATABASE_URL_RESUMO_SEMANAL)
    data_limite = datetime.now() - timedelta(days=delta_tempo)
    sql = sa_text("""
        SELECT summary, "timestamp"
        FROM resumos
        WHERE employee_email = :email
          AND "timestamp" >= :data_limite
        ORDER BY "timestamp" ASC
    """)
    with engine.connect() as conn:
        df = pd.read_sql_query(sql, conn, params={"email": email, "data_limite": data_limite})
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        linhas = [
            f"resumo da semana {ts.strftime('%d/%m/%Y')} - {sm}"
            for ts, sm in zip(df["timestamp"], df["summary"].fillna("").astype(str).str.strip())
            if pd.notnull(ts)
        ]
        resumos_semanal = "\n".join(linhas)
except Exception as e:
    st.error(f"[ERRO] Falha ao buscar resumos_semanal: {e}")

# ==== FORM ====
def pergunta_streamlit(rotulo, valor_atual, data_atual, informacao):
    dias = _dias_desde(_parse_data(data_atual))
    if _is_empty_text(valor_atual):
        resposta = st.text_area(rotulo, key=informacao+"_novo")
        if st.button(f"Salvar {informacao}", key=informacao+"_salvar"):
            st.session_state[informacao] = resposta
            salvar_info(email, informacao, resposta)
    elif dias is None or dias > tempo_atualizacao:
        resposta = st.text_input(
            f"{rotulo}\n(Dado anterior tem {dias} dias). Digite 'sim' se continua válido ou atualize abaixo:",
            key=informacao+"_atualizar"
        )
        if st.button(f"Atualizar {informacao}", key=informacao+"_atualizar_btn"):
            if resposta.strip().lower() != "sim":
                st.session_state[informacao] = resposta
                salvar_info(email, informacao, resposta)
    else:
        st.session_state[informacao] = valor_atual

# ==== EXECUÇÃO STREAMLIT ====
st.title("PDI - Mindsight")
st.subheader(f"Pessoa: {email}")

(
    pontos_fortes, data_pontos_fortes,
    pontos_desenvolvimento, data_pontos_desenvolvimento,
    resumo_avd, data_resumo_avd,
    feedback, data_feedback,
    pdi, data_pdi,
    objetivos, data_objetivos,
    tarefas, data_tarefas,
    diagnostico, data_diag
) = get_latest_infos(email)

# Perguntas dinâmicas
pergunta_streamlit("Aponte resumidamente seus principais pontos fortes:",
                   pontos_fortes, data_pontos_fortes, INFO_TAGS_PF)

pergunta_streamlit("Resumidamente, em quais pontos você precisa se desenvolver?",
                   pontos_desenvolvimento, data_pontos_desenvolvimento, INFO_TAGS_PD)

pergunta_streamlit("Resuma seus principais objetivos de carreira (6–12 meses):",
                   objetivos, data_objetivos, INFO_OBJETIVOS)

st.subheader("Tarefas do cargo")
if cargo_pessoa:
    st.write(f"Cargo: {cargo_pessoa}")
tarefas_cargo = st.session_state.get("tarefas_cargo", "")
resposta_tarefas = st.text_area(
    "Descreva suas tarefas mais importantes, destacando as que tem mais facilidade e as que tem mais dificuldade:",
    value=tarefas or "", key="tarefas_area"
)
if st.button("Salvar tarefas"):
    st.session_state[INFO_TAREFAS] = resposta_tarefas
    salvar_info(email, INFO_TAREFAS, resposta_tarefas)
resultado = resposta_tarefas

# ==== DIAGNÓSTICO ====
campos_ok = all([
    st.session_state.get(INFO_TAGS_PF) or pontos_fortes,
    st.session_state.get(INFO_TAGS_PD) or pontos_desenvolvimento,
    st.session_state.get(INFO_OBJETIVOS) or objetivos,
    st.session_state.get(INFO_TAREFAS) or tarefas
])

if foco == "pdi" and campos_ok:
    st.subheader("Diagnóstico do PDI")

    if st.button("Gerar Diagnóstico com IA"):
        pergunta_prompt = f"""
        Nesse momento, você como especialista deverá fazer um diagnóstico que ajude a pessoa a tomar a decisão do que pode fazer mais sentido se desenvolver.
        para isso, aqui estão algumas informações da pessoa {resumo_pessoa}.
        O feedback, caso a pessoa tenha, foi esse aqui {feedback}.
        os pontos fortes são: {pontos_fortes} e os pontos de desenvolvimento são: {pontos_desenvolvimento}.
        Na tarefa atual essa são as tarefas e um pouco de como ela é: {resultado}.
        e os objetivos são: {objetivos} e que tem o seguinte histórico de interação com você: {historico_bot}.
        Leve em consideração também, para mapear as tarefas e dificuldades os relatórios semanais da pessoa {resumos_semanal}.
        Retorne esse diagnóstico com a seguinte estrutura e oferecendo argumentos e os motivos.
        1- Resumo da pessoa até o momento:
        2- Gaps na posição atual e direcional para a posição atual: 
        3- Futuro dado posição atual e objetivos de carreira:
        4- Indicações de pontos de desenvolvimento:(Citando competências, habilidades e atitudes que dado as informações a pessoa deveria considerar desenvolver, bem como os motivos.)
        """
        sessionId = f"{id_pessoa}:{dt.date.today().isoformat()}"

        try:
            headers = {"Content-Type": "application/json"}
            # Se tiver uma API Key no Flowise, descomente a linha abaixo
            # headers["Authorization"] = f"Bearer {FLOWISE_API_KEY}"

            r = requests.post(
                API_URL,
                json={"question": pergunta_prompt, "overrideConfig": {"sessionId": sessionId}},
                headers=headers,
                timeout=90
            )
            r.raise_for_status()
            output = r.json()

            resposta = (
                output.get("text")
                or output.get("answer")
                or output.get("output")
                or (output["data"][0]["text"] if "data" in output and output["data"] else "")
                or ""
            )

            if not resposta.strip():
                st.warning("⚠️ A API não retornou conteúdo.")
            else:
                st.session_state["diagnostico"] = resposta

        except Exception as e:
            st.error(f"[ERRO] Falha ao gerar diagnóstico: {e}")
            if "r" in locals():
                st.text(r.text)



    if "diagnostico" in st.session_state:
        diag_edit = st.text_area("Edite seu diagnóstico:", value=st.session_state["diagnostico"], height=300)
        if st.button("Salvar Diagnóstico Final"):
            salvar_info(email, INFO_DIAGNOSTICO, diag_edit)
            st.session_state["diagnostico_salvo"] = diag_edit

# ==== COMPETÊNCIAS ====
if st.session_state.get("diagnostico_salvo"):
    st.subheader("Definição de Competências para o PDI")

    comp1 = st.text_input("Competência 1 (obrigatória)", key="Competencia_PDI_1")
    comp2 = st.text_input("Competência 2 (opcional)", key="Competencia_PDI_2")

    if st.button("Salvar Competências"):
        if comp1.strip():
            salvar_info(email, "Competencia_PDI_1", comp1.strip())
        if comp2.strip():
            salvar_info(email, "Competencia_PDI_2", comp2.strip())
        st.success("Competências salvas com sucesso!")

# ==== GERAR PDI ====
if st.session_state.get("Competencia_PDI_1"):
    st.subheader("Plano de Desenvolvimento Individual (PDI)")

    focos_desenvolvimento = [st.session_state["Competencia_PDI_1"]]
    if st.session_state.get("Competencia_PDI_2"):
        focos_desenvolvimento.append(st.session_state["Competencia_PDI_2"])

    if st.button("Gerar PDI com IA"):
        prompt_pdi = f"""
        Você é um especialista em desenvolvimento de carreira e deverá criar um Plano de Desenvolvimento Individual (PDI) de alta qualidade.

        Use as informações do diagnóstico inicial abaixo como base para montar o PDI:

        {st.session_state['diagnostico_salvo']}

        Além disso, utilize as informações reais de {resultado} e {resumos_semanal} para sugerir atividades práticas que façam sentido no contexto do dia a dia da pessoa.

        Estruture o PDI no modelo 70-20-10, separado por competência para os seguintes pontos de desenvolvimento escolhidos pela pessoa {focos_desenvolvimento}.
        Para cada competência identificada no diagnóstico, siga esta estrutura:

        ### Competência: [nome da competência]

        **Objetivo de Desenvolvimento**
        Descreva o objetivo principal para esta competência, resumido em 2-3 linhas.

        **70% Atividades práticas (on the job)**
        Liste de 3 a 5 atividades diretamente conectadas às {resultado} e {resumos_semanal} da pessoa.
        Cada atividade deve ser descrita no formato SMART.

        **20% Aprendizagem com os outros**
        Liste de 2 a 4 atividades informais (mentorias, feedbacks, shadowing etc.), conectadas às {resultado} e {resumos_semanal}, no formato SMART.

        **10% Cursos e treinamentos**
        Indique de 1 a 3 formações formais relacionadas à competência.

        --- Regras ---
        - O PDI deve ter múltiplas competências, cada uma com sua própria estrutura.
        - Nas seções 70% e 20%, use {resultado} e {resumos_semanal} para alinhar à realidade.
        - Todas as metas devem estar no formato SMART.
        - Conecte os objetivos de desenvolvimento ao impacto esperado no negócio.
        """
        sessionId = f"{id_pessoa}:{dt.date.today().isoformat()}"
        r = requests.post(API_URL, json={
            "question": prompt_pdi,
            "overrideConfig": {"sessionId": sessionId}
        })
        output = r.json()
        resposta = output.get("text", "")
        st.session_state["pdi"] = resposta

    if "pdi" in st.session_state:
        pdi_edit = st.text_area("Edite seu PDI:", value=st.session_state["pdi"], height=400)
        if st.button("Salvar PDI Final"):
            # 1. Salva o PDI final no banco
            salvar_info(email, "output_pdi", pdi_edit)

            # 2. Envia para OpenAI no formato solicitado
            prompt_formatado = f"""
            A partir do PDI a seguir, retorne no seguinte formato, mantendo sempre ele:

            'Nome do objetivo 1': (A competência ou o fator que a pessoa deverá desenvolver nesse ciclo);
            'Descrição objetivo 1': (Descrição contida no texto do motivo e o que deve ser feito. Pode manter quase tudo dessa competência);
            'Tarefa 1': Tarefa que no texto diz que deve ser feito;
            'Tarefa 2': Outra tarefa que deve ser feita;
            'Tarefa 3': Outra tarefa que deve ser feita;
            'Tarefa ...': Outra tarefa que deve ser feita;
            Até a tarefa que estiver listada no PDI

            Em seguida, faça o mesmo para a segunda competência, caso exista:
            'Nome do objetivo 2': ...
            'Descrição objetivo 2': ...
            'Tarefa 1': ...
            'Tarefa 2': ...
            'Tarefa ...': Outra tarefa que deve ser feita;
            Até a tarefa que estiver listada no PDI
            PDI fornecido:
            {pdi_edit}
            """

            try:
                # 3. Chama sua API (mesma estrutura que você já usa para gerar PDI normal)
                sessionId = f"{id_pessoa}:{dt.date.today().isoformat()}"
                r = requests.post(API_URL, json={
                    "question": prompt_formatado,
                    "overrideConfig": {"sessionId": sessionId}
                })
                output = r.json()
                pdi_formatado = output.get("text", "").strip()

                # 4. Salva no banco
                salvar_info(email, "output_pdi_formatado", pdi_formatado)

                # 5. Envia e-mail para o usuário
                assunto = "Seu PDI - Mindsight"
                corpo = f"""
                Olá,

                Segue abaixo o resumo formatado do seu PDI:

                {pdi_formatado}

                Segue também o seu PDI completo:
                {pdi_edit}

                Atenciosamente,
                Equipe Mindsight
                """
                if enviar_email_graph(email, assunto, corpo):
                    st.success(f"📧 PDI enviado com sucesso para {email}")

                # 6. Mostra na tela também
                st.success("PDI Final e versão formatada salvos com sucesso! -- VÁ PARA O LINK https://acompanhamento.mindsight.com.br/mindsight/pdi/rodadas E SALVE SEU PDI NO SISTEMA USANDO AS INFOS ABAIXO")
                st.text_area("PDI Formatado:", value=pdi_formatado, height=300)

            except Exception as e:
                st.error(f"[ERRO] Falha ao gerar PDI formatado: {e}")