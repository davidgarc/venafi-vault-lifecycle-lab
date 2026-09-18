import copy, importlib.util, pathlib, unittest
spec=importlib.util.spec_from_file_location('renewal_verify',pathlib.Path(__file__).with_name('verify.py'))
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
class ConvergenceTests(unittest.TestCase):
    def fixture(self):
        pod={'certificate':{'serial':'abc','public_key_sha256':'123','valid':True,'ready':True}}
        return {'experiments':{'agent':{'pods':[copy.deepcopy(pod) for _ in range(3)]}}}
    def test_all_replicas_required(self):
        s=self.fixture();self.assertTrue(v.converged(s,'agent',3));s['experiments']['agent']['pods'][1].pop('certificate');self.assertFalse(v.converged(s,'agent',3))
    def test_missing_replica_fails(self):
        s=self.fixture();s['experiments']['agent']['pods'].pop();self.assertFalse(v.converged(s,'agent',3))
    def test_different_identity_fails(self):
        s=self.fixture();s['experiments']['agent']['pods'][0]['certificate']['serial']='different';self.assertFalse(v.converged(s,'agent',3))
    def test_expired_fails(self):
        s=self.fixture();s['experiments']['agent']['pods'][0]['certificate']['valid']=False;self.assertFalse(v.converged(s,'agent',3))
if __name__=='__main__':unittest.main()
