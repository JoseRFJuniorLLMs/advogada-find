from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from dotenv import load_dotenv

from .app import AdvogadaFind
from .config import Settings
from .catalog import import_cities, export_catalog
from .registry import import_cnpj
from .dashboard import serve
from .receita_sync import sync_cnpj
from .curated import import_curated


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='advogada-find')
    parser.add_argument('--env-file', type=Path, help='Arquivo privado de configuração')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init-db')
    scan = sub.add_parser('scan')
    scan.add_argument('--city', required=True)
    scan.add_argument('--state', required=True)
    for command in ('send-once', 'daemon', 'status', 'companies', 'contacts', 'cities', 'registry',
                    'applications', 'events', 'targets'):
        sub.add_parser(command)
    preview = sub.add_parser('preview', help='Mostra a mensagem sem enviar nem usar o banco')
    preview.add_argument('--company', default='Escritório Exemplo Advogados Associados')
    preview.add_argument('--to', default='contato@exemplo.com.br')
    curated = sub.add_parser('import-curated', help='Verifica e importa lista prioritária de sites oficiais')
    curated.add_argument('--input', type=Path, required=True)
    curated.add_argument('--batch', required=True)
    city_import = sub.add_parser('import-cities')
    scope = city_import.add_mutually_exclusive_group(required=True)
    scope.add_argument('--state', help='UF: SP, RJ, MG...')
    scope.add_argument('--all-states', action='store_true', help='Todos os municípios do Brasil')
    export = sub.add_parser('export-catalog')
    export.add_argument('--output', required=True, type=Path)
    cnpj_import = sub.add_parser('import-cnpj')
    cnpj_import.add_argument('--establishments',nargs='+',type=Path,required=True)
    cnpj_import.add_argument('--municipalities',type=Path,required=True)
    cnpj_import.add_argument('--companies',nargs='+',type=Path,required=True)
    cnpj_import.add_argument('--state',help='Omitir para Brasil inteiro')
    sync=sub.add_parser('sync-cnpj')
    sync.add_argument('--cache-dir',type=Path)
    doctor = sub.add_parser('doctor')
    dashboard = sub.add_parser('dashboard')
    dashboard.add_argument('--port',type=int,default=8797)
    doctor.add_argument('--smtp', action='store_true', help='Autentica sem enviar mensagem')
    add = sub.add_parser('add-city')
    add.add_argument('city')
    add.add_argument('state')
    add.add_argument('--priority', type=int, default=50)
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.env_file:
            if not args.env_file.is_file():
                raise ValueError('Arquivo de configuração não encontrado')
            load_dotenv(args.env_file, override=True)
        app = AdvogadaFind(Settings())
        command = args.command
        if command in {'init-db', 'status'}:
            app.db.recover_interrupted()
            output = app.db.stats()
        elif command == 'scan':
            output = app.scan_city(args.city.strip(), args.state.strip().upper())
        elif command == 'send-once':
            output = app.send_once()
        elif command == 'doctor':
            output = app.doctor(args.smtp)
        elif command == 'preview':
            output = app.preview(' '.join(args.company.split()), args.to.strip())
        elif command == 'targets':
            output = app.db.list_targets(app.s.max_send_attempts)
        elif command == 'companies':
            output = app.db.list_companies()
        elif command == 'contacts':
            output = app.db.list_contacts()
        elif command == 'cities':
            output = app.db.list_cities()
        elif command == 'registry':
            output = app.db.list_registry()
        elif command == 'import-curated':
            output = import_curated(app.db, app.s, args.input, args.batch)
        elif command == 'import-cnpj':
            output = import_cnpj(app.db,args.establishments,args.municipalities,args.companies,args.state)
        elif command == 'sync-cnpj':
            output=sync_cnpj(app.db,args.cache_dir or app.s.db_path.parent/'cnpj-cache')
        elif command == 'import-cities':
            output = {'scope': args.state.upper() if args.state else 'BR', 'cities': import_cities(app.db,args.state,app.s.http_timeout_seconds)}
        elif command == 'export-catalog':
            output = {'rows': export_catalog(app.db,args.output), 'output': str(args.output)}
        elif command == 'applications':
            app.db.recover_interrupted()
            output = app.db.list_applications()
        elif command == 'events':
            output = app.db.recent_events()
        elif command == 'daemon':
            app.daemon()
            return
        elif command == 'dashboard':
            serve(app.s,args.port)
            return
        else:
            app.db.add_city(args.city, args.state, args.priority)
            output = {'added': f'{args.city}/{args.state}', 'priority': args.priority}
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        if (command == 'doctor' and not output['ok']) or (command == 'import-curated' and output['imported'] == 0):
            raise SystemExit(1)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f'Erro: {exc}\n')
