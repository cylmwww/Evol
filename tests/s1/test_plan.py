import copy
import os
import subprocess
from common import Case
from evol.runtime.models import validate
from evol.runtime.service import Runtime

class InputTests(Case):
    def test_A02_invalid_plans(self):
        mutations = [lambda p:p.update(extra=1),lambda p:p['steps'][0].update(tool='shell'),lambda p:p['steps'][0].update(tool=[]),
                     lambda p:p['steps'][1].update(id='read'),lambda p:p['steps'][2]['args'].update(expected_from='verify'),
                     lambda p:p['steps'][0]['args'].update(extra='x')]
        for change in mutations:
            p = copy.deepcopy(self.plan)
            change(p)
            with self.assertRaises(ValueError):
                self.r.create(self.source,p)
        self.assertFalse(self.state.exists())
    def test_A03_limits(self):
        for policy in ({'max_steps':2},{'max_file_bytes':2},{'max_input_bytes':2}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.r.create(self.source,self.plan,policy)
        p=copy.deepcopy(self.plan)
        p['inputs'].append('second.txt')
        with self.assertRaises(ValueError):
            self.r.create(self.source,p,{'max_files':1})
        self.assert_source()
        if self.state.exists():
            self.assertEqual(self.rows('SELECT * FROM runs'),[])
    def test_A04_paths(self):
        for path in ('../x','C:x','/x','//server/share','config.txt:stream','dir/../x','CON','x.','x\\y'):
            p = copy.deepcopy(self.plan)
            p['inputs']=[path]
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate(p)
        p = copy.deepcopy(self.plan)
        p['inputs'].append('CONFIG.TXT')
        with self.assertRaises(ValueError):
            validate(p)
        target = self.base/'external'
        target.mkdir()
        (target/'config.txt').write_bytes(b'external')
        junction = self.source/'junction'
        if os.name == 'nt':
            result = subprocess.run(['cmd','/c','mklink','/J',str(junction),str(target)],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
        else:
            junction.symlink_to(target,target_is_directory=True)
        self.addCleanup(lambda: junction.rmdir() if os.name=='nt' else junction.unlink())
        p=copy.deepcopy(self.plan)
        p['inputs']=['junction/config.txt']
        for s in p['steps']:
            s['args']['path']='junction/config.txt'
        with self.assertRaises(ValueError):
            self.r.create(self.source,p)
    def test_A04_real_symlink(self):
        target=self.base/'external'
        target.mkdir()
        (target/'config.txt').write_bytes(b'external')
        link=self.source/'link.txt'
        try:
            link.symlink_to(target/'config.txt')
        except OSError as exc:
            self.fail('本机符号链接无法创建，路径验收未完成: '+str(exc))
        p=copy.deepcopy(self.plan)
        p['inputs']=['link.txt']
        for s in p['steps']:
            s['args']['path']='link.txt'
        with self.assertRaises(ValueError):
            self.r.create(self.source,p)
    def test_A05_copy_mutation(self):
        def hook(point, detail):
            if point=='copy_read':
                (self.source/detail).write_bytes(b'changed')
        self.r=Runtime(self.state,hook=hook)
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(self.rows('SELECT * FROM runs'),[])
    def test_create_inspect_frozen(self):
        self.create()
        self.assertEqual(self.r.inspect(self.rid)['status'],'queued')
        self.assertEqual(self.file.read_bytes(),(self.source/'config.txt').read_bytes())
        self.assertEqual(self.cli('inspect',self.rid).returncode,0)
        self.assert_source()
